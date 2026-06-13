import os
import threading
import time
from datetime import datetime

import schedule
from flask import Flask

from config import PRODUCT_URL, TELEGRAM_CHAT_ID, validate_config
from notifier import (
    answer_callback_query,
    get_updates,
    send_control_message,
    send_status_alert,
    set_bot_commands,
)
from tracker import is_available

app = Flask(__name__)
check_lock = threading.Lock()

state = {
    "paused": False,
    "notify_only_on_change": False,
    "daily_summary": True,
    "notified_in_stock": False,
    "last_status": None,
    "last_checked": None,
    "last_summary_date": None,
    "interval": int(os.getenv("CHECK_INTERVAL_MINUTES", "15")),
    "telegram_offset": None,
}


def controls():
    return {
        "paused": state["paused"],
        "notify_only_on_change": state["notify_only_on_change"],
        "daily_summary": state["daily_summary"],
    }


def status_label(available):
    if available is True:
        return "available"
    if available is False:
        return "unavailable"
    if available is None:
        return "unclear"
    return "not checked yet"


def control_status_message():
    checked = state["last_checked"] or "never"
    mode = "changes only" if state["notify_only_on_change"] else "every check"
    paused = "yes" if state["paused"] else "no"
    daily = "on" if state["daily_summary"] else "off"

    return (
        "*Tracker status*\n\n"
        f"Stock: `{status_label(state['last_status'])}`\n"
        f"Last check: `{checked}`\n"
        f"Paused: `{paused}`\n"
        f"Interval: `{state['interval']} minutes`\n"
        f"Notifications: `{mode}`\n"
        f"Daily summary: `{daily}`"
    )


def should_send_status(available, previous_status):
    if available is True:
        return not state["notified_in_stock"] or not state["notify_only_on_change"]

    if state["notify_only_on_change"]:
        return previous_status != available

    return True


def should_send_daily_summary(available, already_sending):
    if available is not False or already_sending or not state["daily_summary"]:
        return False

    today = datetime.utcnow().date().isoformat()
    if state["last_summary_date"] == today:
        return False

    state["last_summary_date"] = today
    return True


def run_stock_check(force_notify=False):
    with check_lock:
        previous_status = state["last_status"]
        available = is_available()
        state["last_status"] = available
        state["last_checked"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        send_now = force_notify or should_send_status(available, previous_status)
        send_daily = should_send_daily_summary(available, send_now)

        if send_now or send_daily:
            send_status_alert(available, **controls())

        if available is True:
            state["notified_in_stock"] = True
        elif available is False:
            state["notified_in_stock"] = False

        return available


def scheduled_check():
    if state["paused"]:
        print("Tracker is paused; skipping scheduled check", flush=True)
        return

    run_stock_check()


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


def handle_command(text):
    command = text.split()[0].lower()
    if command == "/start":
        send_control_message(control_status_message(), **controls())
    elif command == "/status":
        send_control_message(control_status_message(), **controls())
    elif command in {"/check", "/refresh"}:
        send_control_message("*Refresh started.*", **controls())
        run_stock_check(force_notify=True)
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
            "/status - show current tracker settings\n"
            "/check - check Amazon now\n"
            "/refresh - check Amazon now\n"
            "/pause - pause scheduled checks\n"
            "/resume - resume scheduled checks",
            **controls(),
        )


def handle_callback(query):
    data = query.get("data", "")
    callback_id = query.get("id")

    if data == "refresh":
        answer_callback_query(callback_id, "Checking now...")
        run_stock_check(force_notify=True)
    elif data == "status":
        answer_callback_query(callback_id, "Sending status")
        send_control_message(control_status_message(), **controls())
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
    elif data == "toggle_daily":
        state["daily_summary"] = not state["daily_summary"]
        mode = "on" if state["daily_summary"] else "off"
        answer_callback_query(callback_id, f"Daily summary: {mode}")
        send_control_message(f"*Daily summary:* `{mode}`", **controls())


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
        except Exception as e:
            print(f"Telegram control loop error: {e}", flush=True)
            time.sleep(10)


@app.get("/")
def health():
    return {
        "status": "running",
        "product_url": PRODUCT_URL,
        "last_stock_status": status_label(state["last_status"]),
        "last_checked": state["last_checked"],
        "paused": state["paused"],
        "interval_minutes": state["interval"],
    }


if __name__ == "__main__":
    validate_config()
    set_bot_commands()
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_telegram_controls, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
