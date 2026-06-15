import argparse
import html
import os
import random
import re
import time

import requests
import schedule

from config import PRODUCT_URL, validate_config
from notifier import send_alert, send_status_alert

REQUEST_TIMEOUT = 25

# Optional residential/proxy support. No effect unless PROXY_URL is set.
PROXY_URL = os.getenv("PROXY_URL")

# IMPORTANT: this is a DESKTOP User-Agent on purpose. The detection patterns
# below look for desktop element IDs (add-to-cart-button, buy-now-button,
# productTitle). A mobile User-Agent makes Amazon serve a different layout
# where those IDs may be missing, which causes false "unclear" results and
# MISSED restock alerts. Keep the UA and the detection layout in sync.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
}
BOT_CHECK_MARKERS = (
    "validatecaptcha",
    "robot check",
    "enter the characters you see below",
)
AVAILABLE_PATTERNS = (
    re.compile(r'id=["\']add-to-cart-button["\']', re.IGNORECASE),
    re.compile(r'id=["\']buy-now-button["\']', re.IGNORECASE),
)
UNAVAILABLE_MARKERS = (
    "currently unavailable",
    "temporarily out of stock",
    "we don't know when or if this item will be back in stock",
)
PRODUCT_TITLE_PATTERN = re.compile(
    r'id=["\']productTitle["\'][^>]*>(.*?)</',
    re.IGNORECASE | re.DOTALL,
)
PAGE_TITLE_PATTERN = re.compile(
    r"<title[^>]*>(.*?)</title>",
    re.IGNORECASE | re.DOTALL,
)
PRICE_CONTAINER_PATTERNS = (
    re.compile(
        r'id=["\'](?:priceblock_ourprice|priceblock_dealprice|priceblock_saleprice|corePriceDisplay_desktop_feature_div)["\'][^>]*>(.*?)</(?:span|div)>',
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r'class=["\'][^"\']*a-price-whole[^"\']*["\'][^>]*>(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    ),
)
RUPEE_PRICE_PATTERN = re.compile(r"(?:₹|&#8377;|&\#x20b9;)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def detect_availability(page_html):
    lowered = page_html.lower()

    if any(marker in lowered for marker in BOT_CHECK_MARKERS):
        print("Amazon returned a bot-check page", flush=True)
        return None

    # Available is checked before unavailable on purpose. For a restock
    # tracker a false "available" costs one wasted click, while a missed
    # restock defeats the whole tool. The bias favors alerting.
    if any(pattern.search(page_html) for pattern in AVAILABLE_PATTERNS):
        return True

    if any(marker in lowered for marker in UNAVAILABLE_MARKERS):
        return False

    return None


def extract_product_title(page_html):
    match = PRODUCT_TITLE_PATTERN.search(page_html) or PAGE_TITLE_PATTERN.search(page_html)
    if not match:
        return None

    title = re.sub(r"<[^>]+>", " ", match.group(1))
    title = html.unescape(title)
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\s*:\s*Amazon\.in.*$", "", title, flags=re.IGNORECASE)
    return title[:180] or None


def extract_price(page_html):
    for container_pattern in PRICE_CONTAINER_PATTERNS:
        for match in container_pattern.finditer(page_html):
            text = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
            text = re.sub(r"\s+", " ", text).strip()
            price_match = RUPEE_PRICE_PATTERN.search(text)
            if price_match:
                return f"₹{price_match.group(1)}"

            whole_number = re.sub(r"[^0-9,]", "", text)
            if whole_number:
                return f"₹{whole_number}"

    return None


def _proxies():
    if PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None


def fetch_product_result(product_url, session=None):
    client = session or requests.Session()
    response = client.get(
        product_url,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
        proxies=_proxies(),
    )
    response.raise_for_status()
    return {
        "available": detect_availability(response.text),
        "title": extract_product_title(response.text),
        "price": extract_price(response.text),
    }


def is_available(product_url=PRODUCT_URL):
    print(f"Checking availability: {product_url}", flush=True)
    try:
        result = fetch_product_result(product_url)
    except requests.RequestException as error:
        print(f"HTTP check failed: {error}", flush=True)
        return None
    available = result["available"]

    if available is True:
        print("IN STOCK!", flush=True)
    elif available is False:
        print("Still unavailable", flush=True)
    else:
        print("Status unclear, will retry", flush=True)
    return available


def check_urls(product_urls):
    results = []
    with requests.Session() as session:
        for index, product_url in enumerate(product_urls):
            # Space requests out so all products are not fetched in one burst.
            # Manual single-product checks pass one URL, so they stay instant.
            if index > 0:
                time.sleep(random.uniform(2, 5))

            print(f"Checking availability: {product_url}", flush=True)
            try:
                result = fetch_product_result(product_url, session=session)
            except requests.RequestException as error:
                print(f"HTTP check failed for {product_url}: {error}", flush=True)
                result = {"available": None, "title": None, "price": None}

            available = result["available"]
            if available is True:
                print("IN STOCK!", flush=True)
            elif available is False:
                print("Still unavailable", flush=True)
            else:
                print("Status unclear, will retry", flush=True)
            results.append(result)
    return results


def check_availability(product_url=PRODUCT_URL):
    available = is_available(product_url)
    if available is True:
        send_alert()
    elif available is False:
        send_status_alert(False)
    else:
        send_status_alert(None)


def main():
    parser = argparse.ArgumentParser(description="Track Amazon product availability.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one availability check and exit.",
    )
    args = parser.parse_args()

    validate_config()

    if args.once:
        check_availability()
        return

    schedule.every(15).minutes.do(check_availability)
    print("Tracker started - checking every 15 mins", flush=True)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
