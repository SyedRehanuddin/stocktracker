import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, PRODUCT_URL

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def build_buttons(paused=False, notify_only_on_change=False):
    pause_text = "Resume" if paused else "Pause"
    pause_action = "resume" if paused else "pause"
    notify_text = "Notify: changes only" if notify_only_on_change else "Notify: every check"

    return {
        "inline_keyboard": [
            [{"text": "Buy on Amazon", "url": PRODUCT_URL}],
            [
                {"text": "Refresh Now", "callback_data": "refresh"},
                {"text": "Status", "callback_data": "status"},
            ],
            [{"text": pause_text, "callback_data": pause_action}],
            [
                {"text": "5m", "callback_data": "interval:5"},
                {"text": "10m", "callback_data": "interval:10"},
                {"text": "15m", "callback_data": "interval:15"},
                {"text": "30m", "callback_data": "interval:30"},
            ],
            [{"text": notify_text, "callback_data": "toggle_notify"}],
        ]
    }


def telegram_request(method, payload):
    response = requests.post(f"{API_URL}/{method}", json=payload, timeout=20)
    if response.status_code != 200:
        print(f"Telegram {method} failed: {response.text}", flush=True)
    return response


def send_telegram_message(
    message,
    paused=False,
    notify_only_on_change=False,
):
    response = telegram_request(
        "sendMessage",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": build_buttons(
                paused=paused,
                notify_only_on_change=notify_only_on_change,
            ),
        },
    )
    if response.status_code == 200:
        print("Telegram message sent!", flush=True)


def send_alert(**controls):
    send_status_alert(True, **controls)


def send_status_alert(available, **controls):
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

    send_telegram_message(msg, **controls)


def send_control_message(message, **controls):
    send_telegram_message(message, **controls)


def answer_callback_query(callback_query_id, text):
    telegram_request(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text,
        },
    )


def get_updates(offset=None):
    payload = {
        "timeout": 25,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset

    response = telegram_request("getUpdates", payload)
    if response.status_code != 200:
        return []
    data = response.json()
    return data.get("result", []) if data.get("ok") else []


def set_bot_commands():
    telegram_request(
        "setMyCommands",
        {
            "commands": [
                {"command": "start", "description": "Show tracker dashboard"},
                {"command": "status", "description": "Show current stock status"},
                {"command": "check", "description": "Check Amazon now"},
                {"command": "refresh", "description": "Refresh stock check"},
                {"command": "pause", "description": "Pause scheduled checks"},
                {"command": "resume", "description": "Resume scheduled checks"},
                {"command": "help", "description": "Show commands and buttons"},
            ]
        },
    )
