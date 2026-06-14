import argparse
import re
import time

import requests
import schedule

from config import PRODUCT_URL, validate_config
from notifier import send_alert, send_status_alert

REQUEST_TIMEOUT = 25
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
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
)


def detect_availability(page_html):
    html = page_html.lower()

    if any(marker in html for marker in BOT_CHECK_MARKERS):
        print("Amazon returned a bot-check page", flush=True)
        return None

    if any(pattern.search(page_html) for pattern in AVAILABLE_PATTERNS):
        return True

    if any(marker in html for marker in UNAVAILABLE_MARKERS):
        return False

    return None


def fetch_availability(product_url, session=None):
    client = session or requests.Session()
    response = client.get(
        product_url,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return detect_availability(response.text)


def is_available(product_url=PRODUCT_URL):
    print(f"Checking availability: {product_url}", flush=True)
    try:
        available = fetch_availability(product_url)
    except requests.RequestException as error:
        print(f"HTTP check failed: {error}", flush=True)
        return None

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
        for product_url in product_urls:
            print(f"Checking availability: {product_url}", flush=True)
            try:
                available = fetch_availability(product_url, session=session)
            except requests.RequestException as error:
                print(f"HTTP check failed for {product_url}: {error}", flush=True)
                available = None

            if available is True:
                print("IN STOCK!", flush=True)
            elif available is False:
                print("Still unavailable", flush=True)
            else:
                print("Status unclear, will retry", flush=True)
            results.append(available)
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
