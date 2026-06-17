import time

import requests
from config import ADMIN_CHAT_ID, TELEGRAM_BOT_TOKEN

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def build_buttons(
    paused=False,
    notify_only_on_change=False,
    interval=None,
    product_url=None,
    extra_rows=None,
):
    pause_text = "✅ ⏸ Pause Tracking" if paused else "⏸ Pause Tracking"
    resume_text = "▶️ Resume Tracking" if paused else "✅ ▶️ Resume Tracking"
    rows = []
    if product_url:
        rows.append([{"text": "Buy on Amazon", "url": product_url}])
    if extra_rows:
        rows.extend(extra_rows)

    rows.extend(
        [
            [{"text": "🔍 Check Products", "callback_data": "check"}],
            [{"text": "📋 Tracker Status", "callback_data": "status"}],
            [{"text": "➕ Add Amazon Product", "callback_data": "add"}],
            [{"text": "🗑 Delete Amazon Product", "callback_data": "delete"}],
            [{"text": "📦 My Products", "callback_data": "list"}],
            [{"text": pause_text, "callback_data": "pause"}],
            [{"text": resume_text, "callback_data": "resume"}],
            [{"text": "⏱ How often to check", "callback_data": "interval_menu"}],
            [{"text": "🔔 Alert settings", "callback_data": "alert_menu"}],
        ]
    )

    return {"inline_keyboard": rows}


def telegram_request(method, payload, timeout=20):
    try:
        response = requests.post(f"{API_URL}/{method}", json=payload, timeout=timeout)
    except requests.RequestException as error:
        print(f"Telegram {method} request error: {error}", flush=True)
        return None
    if response.status_code != 200:
        print(f"Telegram {method} failed: {response.text}", flush=True)
    return response


def send_telegram_message(
    message,
    chat_id=None,
    paused=False,
    notify_only_on_change=False,
    interval=None,
    product_url=None,
    extra_rows=None,
    reply_markup=None,
):
    if reply_markup is None:
        reply_markup = build_buttons(
            paused=paused,
            notify_only_on_change=notify_only_on_change,
            interval=interval,
            product_url=product_url,
            extra_rows=extra_rows,
        )

    payload = {
        "chat_id": chat_id or ADMIN_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup,
    }

    # Retry a few times so a single network blip or transient Telegram error
    # does not silently lose an alert.
    for attempt in range(1, 4):
        response = telegram_request("sendMessage", payload)
        if response is not None and response.status_code == 200:
            print("Telegram message sent!", flush=True)
            return True

        # Markdown rejected (bad formatting characters). Resend once as plain
        # text so the message still gets through. This is most common for
        # error messages that contain raw, unescaped error text.
        if (
            response is not None
            and response.status_code == 400
            and "parse_mode" in payload
        ):
            print("Markdown rejected; resending as plain text", flush=True)
            plain_payload = dict(payload)
            plain_payload.pop("parse_mode", None)
            plain_response = telegram_request("sendMessage", plain_payload)
            if plain_response is not None and plain_response.status_code == 200:
                print("Telegram message sent as plain text", flush=True)
                return True

        if attempt < 3:
            time.sleep(2 * attempt)  # backoff: 2s, then 4s

    print("Telegram message FAILED after retries", flush=True)
    return False


def send_alert(**controls):
    send_status_alert(True, **controls)


def send_status_alert(
    available,
    product_name="Product",
    product_url=None,
    price=None,
    chat_id=None,
    **controls,
):
    price_line = f"\n*Price:* `{price}`\n" if price else ""
    if available is True:
        msg = (
            f"*{product_name} Is Available!*\n\n"
            f"{price_line}"
            "Amazon is showing Buy/Add to Cart options.\n\n"
            "Go fast before it's gone."
        )
    elif available is False:
        msg = (
            f"*{product_name}: Unavailable*\n\n"
            f"{price_line}"
            "Amazon is still showing this product as out of stock.\n\n"
            "I will keep checking."
        )
    else:
        msg = (
            f"*{product_name}: Unclear*\n\n"
            f"{price_line}"
            "Amazon did not show a clear stock status this time.\n\n"
            "I will retry on the next check."
        )

    send_telegram_message(msg, chat_id=chat_id, product_url=product_url, **controls)


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

    # HTTP read timeout MUST be longer than the long-poll timeout above (25s),
    # otherwise the request times out on itself whenever no message arrives,
    # spamming errors and making button responses sluggish.
    response = telegram_request("getUpdates", payload, timeout=35)
    if response is None or response.status_code != 200:
        return []
    data = response.json()
    return data.get("result", []) if data.get("ok") else []


def set_bot_commands():
    user_commands = [
        {"command": "start", "description": "Show tracker dashboard"},
        {"command": "status", "description": "Show current stock status"},
        {"command": "check", "description": "Choose a product to check now"},
        {"command": "add", "description": "Add an Amazon product URL"},
        {"command": "list", "description": "List tracked products"},
        {"command": "rename", "description": "Rename a product by number"},
        {"command": "delete", "description": "Choose a product to delete"},
        {"command": "cancel", "description": "Cancel stuck check state"},
        {"command": "pause", "description": "Pause scheduled checks"},
        {"command": "resume", "description": "Resume scheduled checks"},
        {"command": "help", "description": "Show commands and buttons"},
    ]
    admin_commands = user_commands + [
        {"command": "users", "description": "Admin: list users"},
        {"command": "removeuser", "description": "Admin: remove user access"},
    ]

    telegram_request(
        "setMyCommands",
        {"commands": user_commands},
    )
    telegram_request(
        "setMyCommands",
        {
            "commands": admin_commands,
            "scope": {"type": "chat", "chat_id": ADMIN_CHAT_ID},
        },
    )
