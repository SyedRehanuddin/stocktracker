from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from notifier import send_alert
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
    options.add_argument("--window-size=1365,768")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    driver_path = os.getenv("CHROMEDRIVER_PATH") or get_cached_chromedriver()
    if driver_path:
        return webdriver.Chrome(service=Service(str(driver_path)), options=options)
    return webdriver.Chrome(options=options)


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


def check_availability():
    print("Checking availability...", flush=True)
    driver = get_driver()
    try:
        driver.get(PRODUCT_URL)
        time.sleep(random.uniform(3, 5))
        source = driver.page_source.lower()

        if "currently unavailable" in source:
            print("Still unavailable", flush=True)
        elif "add to cart" in source or "add-to-cart-button" in source:
            print("IN STOCK! Sending alert...", flush=True)
            send_alert()
        else:
            print("Status unclear, will retry", flush=True)

    except Exception as e:
        print(f"Error: {e}", flush=True)
    finally:
        driver.quit()


def is_available():
    print("Checking availability...", flush=True)
    driver = get_driver()
    try:
        driver.get(PRODUCT_URL)
        time.sleep(random.uniform(3, 5))
        source = driver.page_source.lower()

        if "currently unavailable" in source:
            print("Still unavailable", flush=True)
            return False
        if "add to cart" in source or "add-to-cart-button" in source:
            print("IN STOCK!", flush=True)
            return True

        print("Status unclear, will retry", flush=True)
        return None
    except Exception as e:
        print(f"Error: {e}", flush=True)
        return None
    finally:
        driver.quit()


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
            notified_in_stock = False

    schedule.every(15).minutes.do(scheduled_check)

    print("Tracker started - checking every 15 mins", flush=True)
    scheduled_check()

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
