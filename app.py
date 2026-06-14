import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import schedule
from flask import Flask

from config import ADDITIONAL_PRODUCT_URLS, PRODUCT_URL, TELEGRAM_CHAT_ID, validate_config
from notifier import (
    answer_callback_query,
    get_updates,
    send_control_message,
    send_status_alert,
    set_bot_commands,
)
from storage import load_products, save_products
from tracker import check_urls

app = Flask(__name__)
check_lock = threading.Lock()
IST = timezone(timedelta(hours=5, minutes=30))
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def clean_url(url):
    return url.strip().strip("<>").rstrip(").,")


def make_product(url, name=None, index=1):
    return {
        "url": clean_url(url),
        "name": name or f"Product {index}",
        "last_status": None,
        "last_checked": None,
        "notified_in_stock": False,
    }


state = {
    "paused": False,
    "notify_only_on_change": False,
    "check_running": False,
    "check_started_at": None,
    "interval": int(os.getenv("CHECK_INTERVAL_MINUTES", "15")),
    "telegram_offset": None,
    "awaiting_product_url": False,
    "products": [],
}


def default_product_urls():
    urls = [PRODUCT_URL]
    urls.extend(
        [
            clean_url(url)
            for url in ADDITIONAL_PRODUCT_URLS.replace("\n", ",").split(",")
            if clean_url(url)
        ]
    )
    return list(dict.fromkeys(urls))


def normalize_loaded_product(product, index):
    return {
        "url": clean_url(product["url"]),
        "name": product.get("name") or f"Product {index}",
        "last_status": product.get("last_status"),
        "last_checked": product.get("last_checked"),
        "notified_in_stock": bool(product.get("notified_in_stock", False)),
    }


def initialize_products():
    loaded = load_products()
    if loaded:
        state["products"] = [
            normalize_loaded_product(product, index)
            for index, product in enumerate(loaded, start=1)
        ]
        existing_urls = {product["url"] for product in state["products"]}
        for url in default_product_urls():
            if url not in existing_urls:
                state["products"].append(
                    make_product(url, index=len(state["products"]) + 1)
                )
        save_products(state["products"])
        return

    state["products"] = [
        make_product(url, f"Product {index}", index=index)
        for index, url in enumerate(default_product_urls(), start=1)
    ]
    save_products(state["products"])


def reload_products_from_storage():
    loaded = load_products()
    if not loaded:
        return
    state["products"] = [
        normalize_loaded_product(product, index)
        for index, product in enumerate(loaded, start=1)
    ]


def controls():
    return {
        "paused": state["paused"],
        "notify_only_on_change": state["notify_only_on_change"],
    }


def now_ist():
    return datetime.now(IST)


def check_age_seconds():
    if not state["check_started_at"]:
        return 0
    return (now_ist() - state["check_started_at"]).total_seconds()


def clear_stale_check_state():
    if state["check_running"] and check_age_seconds() > 480:
        print("Clearing stale check state", flush=True)
        state["check_running"] = False
        state["check_started_at"] = None


def status_label(available):
    if available is True:
        return "available"
    if available is False:
        return "unavailable"
    if available is None:
        return "unclear"
    return "not checked yet"


def is_amazon_url(url):
    url = clean_url(url).lower()
    return url.startswith(("http://", "https://")) and "amazon." in url


def product_exists(url):
    cleaned = clean_url(url)
    return any(product["url"] == cleaned for product in state["products"])


def add_product(url):
    if not is_amazon_url(url):
        return False, "Please send a valid Amazon product link."
    if product_exists(url):
        return False, "That product is already being tracked."

    state["products"].append(make_product(url, index=len(state["products"]) + 1))
    save_products(state["products"])
    return True, f"Added Product {len(state['products'])}."


def remove_product(number_text):
    try:
        number = int(number_text)
    except ValueError:
        return False, "Use `/remove 2` with the product number from `/list`."

    if number < 1 or number > len(state["products"]):
        return False, "That product number is not in the list."

    if len(state["products"]) == 1:
        return False, "Keep at least one product in the tracker."

    removed = state["products"].pop(number - 1)
    save_products(state["products"])
    return True, f"Removed {removed['name']}."


def product_list_message():
    lines = ["*Tracked products*"]
    for index, product in enumerate(state["products"], start=1):
        status = product_status_label(product)
        checked = product["last_checked"] or "never"
        lines.append(
            f"\n*{index}. {product['name']}*\n"
            f"Status: `{status}`\n"
            f"Last check: `{checked}`\n"
            f"[Buy on Amazon]({product['url']})"
        )
    return "\n".join(lines)


def product_summary_lines():
    lines = []
    for index, product in enumerate(state["products"], start=1):
        checked = product["last_checked"] or "never"
        lines.append(
            f"{index}. {product['name']}: `{product_status_label(product)}` "
            f"at `{checked}`"
        )
    return lines


def product_status_label(product):
    if not product["last_checked"]:
        return "not checked yet"
    return status_label(product["last_status"])


def control_status_message():
    mode = "changes only" if state["notify_only_on_change"] else "every check"
    paused = "yes" if state["paused"] else "no"
    checking = "yes" if state["check_running"] else "no"
    age = int(check_age_seconds())

    return (
        "*Tracker status*\n\n"
        f"Products: `{len(state['products'])}`\n"
        f"Paused: `{paused}`\n"
        f"Check running: `{checking}`\n"
        f"Check age: `{age} seconds`\n"
        f"Interval: `{state['interval']} minutes`\n"
        f"Notifications: `{mode}`\n\n"
        "*Products*\n"
        + "\n".join(product_summary_lines())
    )


def check_summary_message():
    return "*Check finished*\n\n" + "\n".join(product_summary_lines())


def run_manual_check_async():
    clear_stale_check_state()
    if state["check_running"]:
        send_control_message("*A check is already running.*", **controls())
        return

    state["check_running"] = True
    state["check_started_at"] = now_ist()
    send_control_message("*Check started for all products.*", **controls())

    def worker():
        try:
            run_stock_check(force_notify=True, reload_first=True)
            send_control_message(check_summary_message(), **controls())
        except Exception as e:
            print(f"Manual check failed: {e}", flush=True)
            send_control_message(f"*Check failed:* `{e}`", **controls())
        finally:
            state["check_running"] = False
            state["check_started_at"] = None

    threading.Thread(target=worker, daemon=True).start()


def should_send_status(product, available, previous_status):
    if available is True:
        return not product["notified_in_stock"] or not state["notify_only_on_change"]

    if state["notify_only_on_change"]:
        return previous_status != available

    return True


def apply_product_result(product, available, force_notify=False):
    previous_status = product["last_status"]
    product["last_status"] = available
    product["last_checked"] = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    send_now = force_notify or should_send_status(product, available, previous_status)
    if send_now:
        send_status_alert(
            available,
            product_name=product["name"],
            product_url=product["url"],
            **controls(),
        )

    if available is True:
        product["notified_in_stock"] = True
    elif available is False:
        product["notified_in_stock"] = False

    return available


def run_stock_check(force_notify=False, reload_first=False):
    with check_lock:
        if reload_first:
            reload_products_from_storage()
        print(f"Checking {len(state['products'])} products", flush=True)
        products = list(state["products"])
        results = check_urls([product["url"] for product in products])
        for product, available in zip(products, results):
            apply_product_result(product, available, force_notify=force_notify)
        save_products(state["products"])
        return results


def scheduled_check():
    clear_stale_check_state()
    if state["paused"]:
        print("Tracker is paused; skipping scheduled check", flush=True)
        return
    if state["check_running"] or check_lock.locked():
        print("Another check is running; skipping scheduled check", flush=True)
        return

    run_stock_check(reload_first=True)


def reschedule_checks():
    schedule.clear("stock-check")
    schedule.every(state["interval"]).minutes.do(scheduled_check).tag("stock-check")
    print(f"Scheduled checks every {state['interval']} mins", flush=True)


def run_scheduler():
    reschedule_checks()

    print(
        f"Tracker web service started - checking every {state['interval']} mins",
        flush=True,
    )
    scheduled_check()

    while True:
        schedule.run_pending()
        time.sleep(30)


def is_authorized_chat(update):
    message = update.get("message") or update.get("callback_query", {}).get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    return chat_id == str(TELEGRAM_CHAT_ID)


def prompt_for_url():
    state["awaiting_product_url"] = True
    send_control_message(
        "*Send me an Amazon product link.*\n\n"
        "I will add it to the tracker and check it with the others.",
        **controls(),
    )


def handle_product_url(text):
    match = URL_RE.search(text)
    if not match:
        send_control_message("I could not find a URL. Send the Amazon product link.", **controls())
        return

    ok, message = add_product(match.group(0))
    state["awaiting_product_url"] = False
    send_control_message(f"*{message}*\n\nUse `/list` to see tracked products.", **controls())


def handle_command(text):
    parts = text.split()
    command = parts[0].lower()

    if command == "/start":
        reload_products_from_storage()
        send_control_message(control_status_message(), **controls())
    elif command == "/status":
        reload_products_from_storage()
        send_control_message(control_status_message(), **controls())
    elif command == "/list":
        reload_products_from_storage()
        send_control_message(product_list_message(), **controls())
    elif command == "/add":
        if len(parts) > 1:
            handle_product_url(" ".join(parts[1:]))
        else:
            prompt_for_url()
    elif command == "/remove":
        if len(parts) < 2:
            send_control_message("Use `/remove 2` with the number from `/list`.", **controls())
        else:
            ok, message = remove_product(parts[1])
            send_control_message(f"*{message}*", **controls())
    elif command == "/check":
        run_manual_check_async()
    elif command == "/cancel":
        state["check_running"] = False
        state["check_started_at"] = None
        send_control_message("*Check state cleared.*", **controls())
    elif command == "/pause":
        state["paused"] = True
        send_control_message("*Tracker paused.*", **controls())
    elif command == "/resume":
        state["paused"] = False
        send_control_message("*Tracker resumed.*", **controls())
    elif command == "/help":
        send_control_message(
            "*Commands*\n\n"
            "/start - show tracker dashboard\n"
            "/status - show tracker settings\n"
            "/list - list tracked products\n"
            "/add - add an Amazon product URL\n"
            "/remove 2 - remove product number 2\n"
            "/check - check all products now\n"
            "/pause - pause scheduled checks\n"
            "/resume - resume scheduled checks",
            **controls(),
        )


def handle_callback(query):
    data = query.get("data", "")
    callback_id = query.get("id")

    if data == "check":
        answer_callback_query(callback_id, "Check started")
        run_manual_check_async()
    elif data == "status":
        answer_callback_query(callback_id, "Sending status")
        reload_products_from_storage()
        send_control_message(control_status_message(), **controls())
    elif data == "cancel_check":
        state["check_running"] = False
        state["check_started_at"] = None
        answer_callback_query(callback_id, "Check state cleared")
        send_control_message("*Check state cleared.*", **controls())
    elif data == "add":
        answer_callback_query(callback_id, "Send a product link")
        prompt_for_url()
    elif data == "list":
        answer_callback_query(callback_id, "Sending product list")
        reload_products_from_storage()
        send_control_message(product_list_message(), **controls())
    elif data == "pause":
        state["paused"] = True
        answer_callback_query(callback_id, "Paused")
        send_control_message("*Tracker paused.*", **controls())
    elif data == "resume":
        state["paused"] = False
        answer_callback_query(callback_id, "Resumed")
        send_control_message("*Tracker resumed.*", **controls())
    elif data.startswith("interval:"):
        state["interval"] = int(data.split(":", 1)[1])
        reschedule_checks()
        answer_callback_query(callback_id, f"Interval set to {state['interval']}m")
        send_control_message(
            f"*Check interval updated:* `{state['interval']} minutes`",
            **controls(),
        )
    elif data == "toggle_notify":
        state["notify_only_on_change"] = not state["notify_only_on_change"]
        mode = "changes only" if state["notify_only_on_change"] else "every check"
        answer_callback_query(callback_id, f"Notifications: {mode}")
        send_control_message(f"*Notifications set to:* `{mode}`", **controls())


def run_telegram_controls():
    while True:
        try:
            updates = get_updates(state["telegram_offset"])
            for update in updates:
                state["telegram_offset"] = update["update_id"] + 1
                if not is_authorized_chat(update):
                    continue

                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    text = update["message"].get("text", "")
                    if text.startswith("/"):
                        handle_command(text)
                    elif state["awaiting_product_url"] or URL_RE.search(text):
                        handle_product_url(text)
        except Exception as e:
            print(f"Telegram control loop error: {e}", flush=True)
            time.sleep(10)


@app.get("/")
def health():
    return {
        "status": "running",
        "product_count": len(state["products"]),
        "products": [
            {
                "name": product["name"],
                "url": product["url"],
                "last_stock_status": product_status_label(product),
                "last_checked": product["last_checked"],
            }
            for product in state["products"]
        ],
        "paused": state["paused"],
        "interval_minutes": state["interval"],
    }


if __name__ == "__main__":
    validate_config()
    initialize_products()
    set_bot_commands()
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_telegram_controls, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
