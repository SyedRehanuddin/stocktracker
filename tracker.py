from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from notifier import send_alert, send_status_alert
from config import PRODUCT_URL, validate_config
import schedule
import time
import random
import argparse
import os
from pathlib import Path


def get_driver():
    options = Options()
    chrome_bin = os.getenv("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--window-size=1024,768")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    driver_path = os.getenv("CHROMEDRIVER_PATH") or get_cached_chromedriver()
    if driver_path:
        driver = webdriver.Chrome(service=Service(str(driver_path)), options=options)
    else:
        driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(35)
    driver.set_script_timeout(20)
    return driver


def get_cached_chromedriver():
    if os.name != "nt":
        return None

    cache_root = Path.home() / ".wdm" / "drivers" / "chromedriver"
    drivers = sorted(
        cache_root.glob("**/chromedriver.exe"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return drivers[0] if drivers else None


def detect_availability(driver):
    add_to_cart_buttons = driver.find_elements(By.ID, "add-to-cart-button")
    buy_now_buttons = driver.find_elements(By.ID, "buy-now-button")

    if any(button.is_displayed() and button.is_enabled() for button in add_to_cart_buttons):
        return True
    if any(button.is_displayed() and button.is_enabled() for button in buy_now_buttons):
        return True

    visible_status_text = " ".join(
        element.text.lower()
        for selector in ("#availability", "#outOfStock", "#buybox")
        for element in driver.find_elements(By.CSS_SELECTOR, selector)
        if element.is_displayed()
    )

    if "currently unavailable" in visible_status_text:
        return False

    return None


def check_availability(product_url=PRODUCT_URL):
    print("Checking availability...", flush=True)
    driver = get_driver()
    try:
        driver.get(product_url)
        time.sleep(random.uniform(3, 5))
        available = detect_availability(driver)

        if available is True:
            print("IN STOCK! Sending alert...", flush=True)
            send_alert()
        elif available is False:
            print("Still unavailable", flush=True)
        else:
            print("Status unclear, will retry", flush=True)

    except Exception as e:
        print(f"Error: {e}", flush=True)
    finally:
        driver.quit()


def is_available(product_url=PRODUCT_URL):
    print("Checking availability...", flush=True)
    driver = get_driver()
    try:
        driver.get(product_url)
        time.sleep(random.uniform(3, 5))
        available = detect_availability(driver)

        if available is True:
            print("IN STOCK!", flush=True)
            return True
        if available is False:
            print("Still unavailable", flush=True)
            return False

        print("Status unclear, will retry", flush=True)
        return None
    except Exception as e:
        print(f"Error: {e}", flush=True)
        return None
    finally:
        driver.quit()


def check_urls(product_urls):
    driver = get_driver()
    results = []
    try:
        for product_url in product_urls:
            print(f"Checking availability: {product_url}", flush=True)
            try:
                driver.get(product_url)
                time.sleep(random.uniform(1.5, 3))
                available = detect_availability(driver)

                if available is True:
                    print("IN STOCK!", flush=True)
                elif available is False:
                    print("Still unavailable", flush=True)
                else:
                    print("Status unclear, will retry", flush=True)

                results.append(available)
            except Exception as e:
                print(f"Error checking {product_url}: {e}", flush=True)
                results.append(None)
    finally:
        driver.quit()

    return results


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

    notified_in_stock = False

    def scheduled_check():
        nonlocal notified_in_stock
        available = is_available()
        if available is True and not notified_in_stock:
            print("Sending Telegram alert...", flush=True)
            send_alert()
            notified_in_stock = True
        elif available is False:
            print("Sending unavailable Telegram status...", flush=True)
            send_status_alert(False)
            notified_in_stock = False
        elif available is None:
            print("Sending unclear Telegram status...", flush=True)
            send_status_alert(None)

    schedule.every(15).minutes.do(scheduled_check)

    print("Tracker started - checking every 15 mins", flush=True)
    scheduled_check()

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
