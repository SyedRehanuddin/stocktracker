import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRODUCT_URL


def send_alert():
    msg = (
        "*KEYBOARD IS BACK!*\n\n"
        "Evofox Katana S Mini Black is NOW available!\n\n"
        f"[Buy NOW]({PRODUCT_URL})\n\n"
        "Go fast before it's gone!"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
        },
        timeout=20,
    )
    if response.status_code == 200:
        print("Telegram alert sent!")
    else:
        print(f"Alert failed: {response.text}")
