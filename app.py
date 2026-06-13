import os
import threading
import time

import schedule
from flask import Flask

from config import PRODUCT_URL, validate_config
from notifier import send_status_alert
from tracker import is_available

app = Flask(__name__)
notified_in_stock = False


def scheduled_check():
    global notified_in_stock

    available = is_available()
    if available is True and not notified_in_stock:
        print("Sending Telegram alert...", flush=True)
        send_status_alert(True)
        notified_in_stock = True
    elif available is False:
        print("Sending unavailable Telegram status...", flush=True)
        send_status_alert(False)
        notified_in_stock = False
    elif available is None:
        print("Sending unclear Telegram status...", flush=True)
        send_status_alert(None)


def run_scheduler():
    interval = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
    schedule.every(interval).minutes.do(scheduled_check)

    print(f"Tracker web service started - checking every {interval} mins", flush=True)
    scheduled_check()

    while True:
        schedule.run_pending()
        time.sleep(30)


@app.get("/")
def health():
    return {
        "status": "running",
        "product_url": PRODUCT_URL,
    }


if __name__ == "__main__":
    validate_config()
    threading.Thread(target=run_scheduler, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
