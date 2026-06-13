import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRODUCT_URL, TRACKER_URL


def build_buttons():
    buttons = [[{"text": "Buy on Amazon", "url": PRODUCT_URL}]]
    if TRACKER_URL:
        buttons.append([{"text": "Open Tracker", "url": TRACKER_URL}])
    return {"inline_keyboard": buttons}


def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": build_buttons(),
        },
        timeout=20,
    )
    if response.status_code == 200:
        print("Telegram alert sent!", flush=True)
    else:
        print(f"Alert failed: {response.text}", flush=True)


def send_alert():
    send_status_alert(True)


def send_status_alert(available):
    if available is True:
        msg = (
            "*KEYBOARD IS BACK!*\n\n"
            "Evofox Katana S Mini Black is NOW available.\n\n"
            "Go fast before it's gone."
        )
    elif available is False:
        msg = (
            "*Keyboard status: unavailable*\n\n"
            "Evofox Katana S Mini Black is still out of stock.\n\n"
            "I will keep checking."
        )
    else:
        msg = (
            "*Keyboard status: unclear*\n\n"
            "Amazon did not show a clear stock status this time.\n\n"
            "I will retry on the next check."
        )

    send_telegram_message(msg)
