import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import schedule
from flask import Flask

from config import (
    ADMIN_CHAT_ID,
    MAX_PRODUCTS_PER_USER,
    MAX_UNIQUE_CHECKS_PER_CYCLE,
    MAX_USERS,
    MIN_CHECK_INTERVAL_MINUTES,
    validate_config,
)
from notifier import (
    answer_callback_query,
    get_updates,
    send_control_message,
    send_status_alert,
    send_telegram_message,
    set_bot_commands,
)
from storage import (
    add_approved_user,
    add_pending_user,
    add_rejected_user,
    is_approved_user,
    is_pending_user,
    is_rejected_user,
    list_approved_users,
    list_pending_users,
    list_rejected_users,
    load_user_products,
    load_user_profile,
    load_user_settings,
    remove_approved_user,
    save_user_products,
    save_user_profile,
    save_user_settings,
)
from tracker import check_urls

app = Flask(__name__)
check_lock = threading.Lock()
schedule_lock = threading.Lock()
IST = timezone(timedelta(hours=5, minutes=30))
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
PRODUCT_NAME_LIMIT = 500

state = {
    "check_running": False,
    "check_started_at": None,
    "telegram_offset": None,
    "awaiting_product_url": set(),
}


def clean_url(url):
    return url.strip().strip("<>").rstrip(").,")


def clean_product_name(name):
    if not name:
        return None
    cleaned = re.sub(r"[*_`\[\]]", "", name).strip()
    if len(cleaned) <= PRODUCT_NAME_LIMIT:
        return cleaned or None

    trimmed = cleaned[: PRODUCT_NAME_LIMIT - 3].rsplit(" ", 1)[0].strip()
    return f"{trimmed}..." if trimmed else cleaned[:PRODUCT_NAME_LIMIT]


def now_ist():
    return datetime.now(IST)


def now_text():
    return now_ist().strftime("%d %b %Y, %I:%M %p IST")


def now_epoch():
    return int(time.time())


def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_CHAT_ID)


def default_settings():
    return {
        "paused": False,
        "notify_only_on_change": False,
        "interval": MIN_CHECK_INTERVAL_MINUTES,
        "last_scheduled_check_epoch": 0,
    }


def normalize_settings(settings):
    normalized = default_settings()
    normalized.update(settings or {})
    normalized["interval"] = max(
        int(normalized.get("interval", MIN_CHECK_INTERVAL_MINUTES)),
        MIN_CHECK_INTERVAL_MINUTES,
    )
    normalized["paused"] = bool(normalized.get("paused", False))
    normalized["notify_only_on_change"] = bool(
        normalized.get("notify_only_on_change", False)
    )
    normalized["last_scheduled_check_epoch"] = int(
        normalized.get("last_scheduled_check_epoch", 0) or 0
    )
    return normalized


def get_user_settings(chat_id):
    settings = normalize_settings(load_user_settings(chat_id))
    save_user_settings(chat_id, settings)
    return settings


def make_product(url, name=None, index=1):
    return {
        "url": clean_url(url),
        "name": clean_product_name(name) or f"Product {index}",
        "last_status": None,
        "last_checked": None,
        "last_success_epoch": None,
        "last_price": None,
        "notified_in_stock": False,
    }


def normalize_product(product, index):
    return {
        "url": clean_url(product["url"]),
        "name": clean_product_name(product.get("name")) or f"Product {index}",
        "last_status": product.get("last_status"),
        "last_checked": product.get("last_checked"),
        "last_success_epoch": product.get("last_success_epoch"),
        "last_price": product.get("last_price"),
        "notified_in_stock": bool(product.get("notified_in_stock", False)),
    }


def get_user_products(chat_id):
    products = [
        normalize_product(product, index)
        for index, product in enumerate(load_user_products(chat_id), start=1)
    ]
    save_user_products(chat_id, products)
    return products


def is_amazon_url(url):
    url = clean_url(url).lower()
    return url.startswith(("http://", "https://")) and "amazon." in url


def status_label(available):
    if available is True:
        return "available"
    if available is False:
        return "unavailable"
    if available is None:
        return "unclear"
    return "not checked yet"


def product_status_label(product):
    if not product["last_checked"]:
        return "not checked yet"
    return status_label(product["last_status"])


def product_status_block(index, product):
    checked = product["last_checked"] or "never"
    price = product.get("last_price") or "not found"
    return (
        f"*Product {index}*\n"
        f"Name: {product['name']}\n"
        f"Status: `{product_status_label(product)}`\n"
        f"Price: `{price}`\n"
        f"Last checked: `{checked}`"
    )


def controls(settings):
    return {
        "paused": settings["paused"],
        "notify_only_on_change": settings["notify_only_on_change"],
        "interval": settings["interval"],
    }


def product_exists(products, url):
    cleaned = clean_url(url)
    return any(product["url"] == cleaned for product in products)


def max_products_for(chat_id):
    return None if is_admin(chat_id) else MAX_PRODUCTS_PER_USER


def add_product(chat_id, url):
    products = get_user_products(chat_id)
    limit = max_products_for(chat_id)
    if not is_amazon_url(url):
        return False, "Please send a valid Amazon product link."
    if product_exists(products, url):
        return False, "That product is already being tracked."
    if limit is not None and len(products) >= limit:
        return False, f"Maximum {limit} products allowed. Remove one before adding another."

    products.append(make_product(url, index=len(products) + 1))
    save_user_products(chat_id, products)
    return True, f"Added Product {len(products)}."


def remove_product(chat_id, number_text):
    products = get_user_products(chat_id)
    try:
        number = int(number_text)
    except ValueError:
        return False, "Use `/remove 2` with the product number from `/list`."

    if number < 1 or number > len(products):
        return False, "That product number is not in the list."

    removed = products.pop(number - 1)
    save_user_products(chat_id, products)
    return True, f"Removed {removed['name']}."


def product_list_message(chat_id):
    products = get_user_products(chat_id)
    if not products:
        return "*Tracked products*\n\nNo products yet. Use `/add` to add one."

    lines = ["*Tracked products*"]
    for index, product in enumerate(products, start=1):
        lines.append(f"\n{product_status_block(index, product)}")
        lines.append(f"[Buy on Amazon]({product['url']})")
    return "\n".join(lines)


def product_summary_lines(chat_id):
    return [
        product_status_block(index, product)
        for index, product in enumerate(get_user_products(chat_id), start=1)
    ]


def check_age_seconds():
    if not state["check_started_at"]:
        return 0
    return (now_ist() - state["check_started_at"]).total_seconds()


def clear_stale_check_state():
    if state["check_running"] and check_age_seconds() > 480:
        print("Clearing stale check state", flush=True)
        state["check_running"] = False
        state["check_started_at"] = None


def begin_check():
    clear_stale_check_state()
    if not check_lock.acquire(blocking=False):
        return False
    state["check_running"] = True
    state["check_started_at"] = now_ist()
    return True


def finish_check():
    state["check_running"] = False
    state["check_started_at"] = None
    if check_lock.locked():
        check_lock.release()


def clear_check_state_only():
    state["check_running"] = False
    state["check_started_at"] = None


def control_status_message(chat_id):
    settings = get_user_settings(chat_id)
    mode = "changes only" if settings["notify_only_on_change"] else "every check"
    paused = "yes" if settings["paused"] else "no"
    checking = "yes" if state["check_running"] else "no"
    age = int(check_age_seconds())
    products = product_summary_lines(chat_id)
    product_text = "\n\n".join(products) if products else "No products yet."

    return (
        "*Tracker status*\n\n"
        f"Products: `{len(get_user_products(chat_id))}`\n"
        f"Paused: `{paused}`\n"
        f"Check running: `{checking}`\n"
        f"Check age: `{age} seconds`\n"
        f"Interval: `{settings['interval']} minutes`\n"
        f"Notifications: `{mode}`\n\n"
        "*Products*\n"
        + product_text
    )


def product_check_rows(chat_id):
    rows = []
    row = []
    for index, _product in enumerate(get_user_products(chat_id), start=1):
        row.append({"text": f"Check {index}", "callback_data": f"check_product:{index}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def check_picker_message(chat_id):
    products = product_summary_lines(chat_id)
    if not products:
        return "*Choose product to check*\n\nNo products yet. Use `/add` to add one."
    return "*Choose product to check*\n\n" + "\n\n".join(products)


def send_check_picker(chat_id):
    settings = get_user_settings(chat_id)
    send_control_message(
        check_picker_message(chat_id),
        chat_id=chat_id,
        extra_rows=product_check_rows(chat_id),
        **controls(settings),
    )


def product_number(products, product):
    for index, tracked_product in enumerate(products, start=1):
        if tracked_product["url"] == product["url"]:
            return index
    return None


def should_send_status(settings, product, available, previous_status):
    if available is True:
        return not product["notified_in_stock"] or not settings["notify_only_on_change"]
    if settings["notify_only_on_change"]:
        return previous_status != available
    return True


def apply_product_result(chat_id, products, product, result, force_notify=False):
    settings = get_user_settings(chat_id)
    if isinstance(result, dict):
        available = result.get("available")
        title = clean_product_name(result.get("title"))
        price = result.get("price")
    else:
        available = result
        title = None
        price = None

    previous_status = product["last_status"]
    if title:
        product["name"] = title
    product["last_price"] = price
    product["last_status"] = available
    product["last_checked"] = now_text()
    if available is True or available is False:
        product["last_success_epoch"] = now_epoch()

    send_now = force_notify or should_send_status(settings, product, available, previous_status)
    if send_now:
        index = product_number(products, product)
        notification_name = (
            f"Product {index} - {product['name']}" if index else product["name"]
        )
        send_status_alert(
            available,
            product_name=notification_name,
            product_url=product["url"],
            price=product.get("last_price"),
            chat_id=chat_id,
            **controls(settings),
        )

    if available is True:
        product["notified_in_stock"] = True
    elif available is False:
        product["notified_in_stock"] = False
    return available


def run_single_product_check(chat_id, product_index, force_notify=False):
    products = get_user_products(chat_id)
    if product_index < 0 or product_index >= len(products):
        return None

    product = products[product_index]
    print(f"Checking {chat_id} {product['name']}: {product['url']}", flush=True)
    results = check_urls([product["url"]])
    result = results[0] if results else {"available": None, "title": None}
    apply_product_result(chat_id, products, product, result, force_notify=force_notify)
    save_user_products(chat_id, products)
    return product


def single_check_summary_message(chat_id, product):
    products = get_user_products(chat_id)
    index = product_number(products, product) or "?"
    return (
        "*Check finished*\n\n"
        f"{product_status_block(index, product)}\n"
        f"[Buy on Amazon]({product['url']})"
    )


def run_single_product_check_async(chat_id, product_index):
    settings = get_user_settings(chat_id)
    if not begin_check():
        send_control_message("*A check is already running.*", chat_id=chat_id, **controls(settings))
        return

    products = get_user_products(chat_id)
    if product_index < 0 or product_index >= len(products):
        finish_check()
        send_control_message("*That product number is not in the list.*", chat_id=chat_id, **controls(settings))
        return

    product = products[product_index]
    send_control_message(f"*Check started:* {product['name']}", chat_id=chat_id, **controls(settings))

    def worker():
        try:
            checked_product = run_single_product_check(
                chat_id,
                product_index,
                force_notify=True,
            )
            if checked_product:
                send_control_message(
                    single_check_summary_message(chat_id, checked_product),
                    chat_id=chat_id,
                    **controls(get_user_settings(chat_id)),
                )
        except Exception as e:
            print(f"Manual check failed: {e}", flush=True)
            send_control_message(f"*Check failed:* `{e}`", chat_id=chat_id, **controls(settings))
        finally:
            finish_check()

    threading.Thread(target=worker, daemon=True).start()


def settings_due(settings):
    last = int(settings.get("last_scheduled_check_epoch", 0) or 0)
    return now_epoch() - last >= settings["interval"] * 60


def scheduled_check():
    if not begin_check():
        print("Another check is running; skipping scheduled check", flush=True)
        return

    try:
        due = []
        url_map = {}
        for chat_id in list_approved_users():
            settings = get_user_settings(chat_id)
            if settings["paused"] or not settings_due(settings):
                continue
            products = get_user_products(chat_id)
            due.append((chat_id, settings, products))
            for index, product in enumerate(products):
                if len(url_map) >= MAX_UNIQUE_CHECKS_PER_CYCLE and product["url"] not in url_map:
                    continue
                url_map.setdefault(product["url"], []).append((chat_id, index))

        if not due:
            return
        if not url_map:
            for chat_id, settings, _products in due:
                settings["last_scheduled_check_epoch"] = now_epoch()
                save_user_settings(chat_id, settings)
            return

        urls = list(url_map)
        print(f"Scheduled check for {len(urls)} unique URLs", flush=True)
        results = dict(zip(urls, check_urls(urls)))
        products_by_user = {chat_id: products for chat_id, _settings, products in due}

        touched_users = set()
        for url, targets in url_map.items():
            result = results.get(url, {"available": None, "title": None})
            for chat_id, index in targets:
                products = products_by_user[chat_id]
                if index >= len(products):
                    continue
                apply_product_result(chat_id, products, products[index], result)
                touched_users.add(chat_id)

        for chat_id, settings, products in due:
            if chat_id in touched_users:
                save_user_products(chat_id, products)
            settings["last_scheduled_check_epoch"] = now_epoch()
            save_user_settings(chat_id, settings)
    finally:
        finish_check()


def reschedule_checks():
    with schedule_lock:
        schedule.clear("stock-check")
        schedule.every(1).minutes.do(scheduled_check).tag("stock-check")
    print("Scheduled multi-user checks every 1 min due-scan", flush=True)


def run_scheduler():
    reschedule_checks()
    print("Tracker web service started", flush=True)
    while True:
        with schedule_lock:
            schedule.run_pending()
        time.sleep(30)


def chat_id_from_message(message):
    return str(message.get("chat", {}).get("id", ""))


def profile_from_message(message, status="pending"):
    user = message.get("from", {}) or {}
    chat_id = chat_id_from_message(message)
    return {
        "chat_id": chat_id,
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", ""),
        "username": user.get("username", ""),
        "status": status,
        "requested_at": now_text(),
    }


def display_name(profile):
    name = " ".join(
        part for part in [profile.get("first_name"), profile.get("last_name")] if part
    ).strip()
    return name or "Unknown"


def bootstrap_admin():
    admin_id = str(ADMIN_CHAT_ID)
    add_approved_user(admin_id)
    profile = load_user_profile(admin_id) or {}
    profile.update(
        {
            "chat_id": admin_id,
            "status": "approved",
            "approved_at": profile.get("approved_at") or now_text(),
            "approved_by": admin_id,
            "username": profile.get("username", "admin"),
        }
    )
    save_user_profile(admin_id, profile)
    if not load_user_settings(admin_id):
        save_user_settings(admin_id, default_settings())
    if load_user_products(admin_id) is None:
        save_user_products(admin_id, [])


def approved_friend_count():
    return len([chat_id for chat_id in list_approved_users() if not is_admin(chat_id)])


def approval_buttons(chat_id):
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"approve:{chat_id}"},
                {"text": "Reject", "callback_data": f"reject:{chat_id}"},
            ]
        ]
    }


def request_access(message):
    chat_id = chat_id_from_message(message)
    if is_approved_user(chat_id):
        send_control_message(control_status_message(chat_id), chat_id=chat_id, **controls(get_user_settings(chat_id)))
        return
    if is_rejected_user(chat_id):
        send_telegram_message("*Access denied.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
        return
    if is_pending_user(chat_id):
        send_telegram_message("*Access request already sent to admin.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
        return
    if approved_friend_count() >= MAX_USERS:
        send_telegram_message("*Access is full right now.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
        return

    profile = profile_from_message(message)
    save_user_profile(chat_id, profile)
    add_pending_user(chat_id)
    send_telegram_message("*Access request sent to admin. Please wait.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})

    username = f"@{profile['username']}" if profile.get("username") else "none"
    admin_message = (
        "*New access request*\n\n"
        f"Name: `{display_name(profile)}`\n"
        f"Username: `{username}`\n"
        f"Chat ID: `{chat_id}`"
    )
    send_telegram_message(
        admin_message,
        chat_id=ADMIN_CHAT_ID,
        reply_markup=approval_buttons(chat_id),
    )


def approve_user(chat_id):
    if approved_friend_count() >= MAX_USERS and not is_approved_user(chat_id):
        send_telegram_message("*User limit reached. Remove a user first.*", chat_id=ADMIN_CHAT_ID, reply_markup={"inline_keyboard": []})
        return
    profile = load_user_profile(chat_id) or {"chat_id": str(chat_id)}
    profile.update(
        {
            "status": "approved",
            "approved_at": now_text(),
            "approved_by": str(ADMIN_CHAT_ID),
        }
    )
    save_user_profile(chat_id, profile)
    add_approved_user(chat_id)
    if not load_user_settings(chat_id):
        save_user_settings(chat_id, default_settings())
    if not load_user_products(chat_id):
        save_user_products(chat_id, [])
    send_telegram_message("*Access approved. Use /start to open tracker.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
    send_telegram_message(f"*Approved user:* `{chat_id}`", chat_id=ADMIN_CHAT_ID, reply_markup={"inline_keyboard": []})


def reject_user(chat_id):
    profile = load_user_profile(chat_id) or {"chat_id": str(chat_id)}
    profile["status"] = "rejected"
    profile["rejected_at"] = now_text()
    save_user_profile(chat_id, profile)
    add_rejected_user(chat_id)
    send_telegram_message("*Access denied.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
    send_telegram_message(f"*Rejected user:* `{chat_id}`", chat_id=ADMIN_CHAT_ID, reply_markup={"inline_keyboard": []})


def users_message():
    approved = list_approved_users()
    pending = list_pending_users()
    rejected = list_rejected_users()
    lines = ["*Users*"]
    lines.append(f"\nApproved: `{len(approved)}`")
    for chat_id in approved:
        profile = load_user_profile(chat_id)
        label = "admin" if is_admin(chat_id) else "user"
        lines.append(f"- `{chat_id}` ({label}) {display_name(profile)}")
    lines.append(f"\nPending: `{len(pending)}`")
    for chat_id in pending:
        profile = load_user_profile(chat_id)
        lines.append(f"- `{chat_id}` {display_name(profile)}")
    lines.append(f"\nRejected: `{len(rejected)}`")
    for chat_id in rejected:
        profile = load_user_profile(chat_id)
        lines.append(f"- `{chat_id}` {display_name(profile)}")
    return "\n".join(lines)


def ensure_authorized(chat_id):
    return is_approved_user(chat_id)


def prompt_for_url(chat_id):
    state["awaiting_product_url"].add(str(chat_id))
    send_control_message(
        "*Send me an Amazon product link.*\n\nI will add it to your tracker.",
        chat_id=chat_id,
        **controls(get_user_settings(chat_id)),
    )


def handle_product_url(chat_id, text):
    match = URL_RE.search(text)
    if not match:
        send_control_message("I could not find a URL. Send the Amazon product link.", chat_id=chat_id, **controls(get_user_settings(chat_id)))
        return

    ok, message = add_product(chat_id, match.group(0))
    state["awaiting_product_url"].discard(str(chat_id))
    send_control_message(f"*{message}*\n\nUse `/list` to see tracked products.", chat_id=chat_id, **controls(get_user_settings(chat_id)))


def handle_command(message):
    text = message.get("text", "")
    parts = text.split()
    command = parts[0].lower()
    chat_id = chat_id_from_message(message)

    if command == "/start":
        if is_admin(chat_id):
            bootstrap_admin()
        if ensure_authorized(chat_id):
            send_control_message(control_status_message(chat_id), chat_id=chat_id, **controls(get_user_settings(chat_id)))
        else:
            request_access(message)
        return

    if not ensure_authorized(chat_id):
        send_telegram_message("*Access not approved yet.*\n\nSend /start to request access.", chat_id=chat_id, reply_markup={"inline_keyboard": []})
        return

    settings = get_user_settings(chat_id)
    if command == "/status":
        send_control_message(control_status_message(chat_id), chat_id=chat_id, **controls(settings))
    elif command == "/list":
        send_control_message(product_list_message(chat_id), chat_id=chat_id, **controls(settings))
    elif command == "/add":
        if len(parts) > 1:
            handle_product_url(chat_id, " ".join(parts[1:]))
        else:
            prompt_for_url(chat_id)
    elif command == "/remove":
        if len(parts) < 2:
            send_control_message("Use `/remove 2` with the number from `/list`.", chat_id=chat_id, **controls(settings))
        else:
            ok, message = remove_product(chat_id, parts[1])
            send_control_message(f"*{message}*", chat_id=chat_id, **controls(settings))
    elif command == "/check":
        send_check_picker(chat_id)
    elif command == "/cancel":
        clear_check_state_only()
        send_control_message("*Check state cleared.*", chat_id=chat_id, **controls(settings))
    elif command == "/pause":
        settings["paused"] = True
        save_user_settings(chat_id, settings)
        send_control_message("*Tracker paused.*", chat_id=chat_id, **controls(settings))
    elif command == "/resume":
        settings["paused"] = False
        save_user_settings(chat_id, settings)
        send_control_message("*Tracker resumed.*", chat_id=chat_id, **controls(settings))
    elif command == "/users" and is_admin(chat_id):
        send_telegram_message(users_message(), chat_id=chat_id)
    elif command == "/removeuser" and is_admin(chat_id):
        if len(parts) < 2:
            send_telegram_message("Use `/removeuser 123456789`.", chat_id=chat_id)
        else:
            remove_approved_user(parts[1])
            profile = load_user_profile(parts[1]) or {"chat_id": parts[1]}
            profile["status"] = "removed"
            profile["removed_at"] = now_text()
            save_user_profile(parts[1], profile)
            send_telegram_message(f"*Removed user access:* `{parts[1]}`", chat_id=chat_id)
    elif command == "/help":
        extra = "\n/users - list users\n/removeuser 123 - remove user access" if is_admin(chat_id) else ""
        send_control_message(
            "*Commands*\n\n"
            "/start - show tracker dashboard\n"
            "/status - show tracker settings\n"
            "/list - list tracked products\n"
            "/add - add an Amazon product URL\n"
            "/remove 2 - remove product number 2\n"
            "/check - choose a product to check now\n"
            "/pause - pause scheduled checks\n"
            "/resume - resume scheduled checks"
            f"{extra}",
            chat_id=chat_id,
            **controls(settings),
        )


def handle_callback(query):
    data = query.get("data", "")
    callback_id = query.get("id")
    message = query.get("message", {})
    chat_id = chat_id_from_message(message)

    if data.startswith("approve:") and is_admin(chat_id):
        target = data.split(":", 1)[1]
        approve_user(target)
        answer_callback_query(callback_id, "Approved")
        return
    if data.startswith("reject:") and is_admin(chat_id):
        target = data.split(":", 1)[1]
        reject_user(target)
        answer_callback_query(callback_id, "Rejected")
        return

    if not ensure_authorized(chat_id):
        answer_callback_query(callback_id, "Access not approved")
        send_telegram_message("*Access not approved yet.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
        return

    settings = get_user_settings(chat_id)
    if data == "check":
        answer_callback_query(callback_id, "Choose a product")
        send_check_picker(chat_id)
    elif data.startswith("check_product:"):
        product_number = int(data.split(":", 1)[1])
        answer_callback_query(callback_id, f"Checking Product {product_number}")
        run_single_product_check_async(chat_id, product_number - 1)
    elif data == "status":
        answer_callback_query(callback_id, "Sending status")
        send_control_message(control_status_message(chat_id), chat_id=chat_id, **controls(settings))
    elif data == "cancel_check":
        clear_check_state_only()
        answer_callback_query(callback_id, "Check state cleared")
        send_control_message("*Check state cleared.*", chat_id=chat_id, **controls(settings))
    elif data == "add":
        answer_callback_query(callback_id, "Send a product link")
        prompt_for_url(chat_id)
    elif data == "list":
        answer_callback_query(callback_id, "Sending product list")
        send_control_message(product_list_message(chat_id), chat_id=chat_id, **controls(settings))
    elif data == "pause":
        settings["paused"] = True
        save_user_settings(chat_id, settings)
        answer_callback_query(callback_id, "Paused")
        send_control_message("*Tracker paused.*", chat_id=chat_id, **controls(settings))
    elif data == "resume":
        settings["paused"] = False
        save_user_settings(chat_id, settings)
        answer_callback_query(callback_id, "Resumed")
        send_control_message("*Tracker resumed.*", chat_id=chat_id, **controls(settings))
    elif data.startswith("interval:"):
        settings["interval"] = max(int(data.split(":", 1)[1]), MIN_CHECK_INTERVAL_MINUTES)
        save_user_settings(chat_id, settings)
        answer_callback_query(callback_id, f"Interval set to {settings['interval']}m")
        send_control_message(
            f"*Check interval updated:* `{settings['interval']} minutes`",
            chat_id=chat_id,
            **controls(settings),
        )
    elif data in ("toggle_notify", "notify:every", "notify:changes"):
        if data == "notify:every":
            settings["notify_only_on_change"] = False
        elif data == "notify:changes":
            settings["notify_only_on_change"] = True
        else:
            settings["notify_only_on_change"] = not settings["notify_only_on_change"]
        save_user_settings(chat_id, settings)
        mode = "changes only" if settings["notify_only_on_change"] else "every check"
        answer_callback_query(callback_id, f"Notifications: {mode}")
        send_control_message(f"*Notifications set to:* `{mode}`", chat_id=chat_id, **controls(settings))


def run_telegram_controls():
    while True:
        try:
            updates = get_updates(state["telegram_offset"])
            for update in updates:
                state["telegram_offset"] = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    message = update["message"]
                    text = message.get("text", "")
                    chat_id = chat_id_from_message(message)
                    if text.startswith("/"):
                        handle_command(message)
                    elif ensure_authorized(chat_id) and (
                        str(chat_id) in state["awaiting_product_url"] or URL_RE.search(text)
                    ):
                        handle_product_url(chat_id, text)
        except Exception as e:
            print(f"Telegram control loop error: {e}", flush=True)
            time.sleep(10)


def scraper_health_summary():
    approved = list_approved_users()
    all_products = []
    min_interval = MIN_CHECK_INTERVAL_MINUTES
    for chat_id in approved:
        settings = get_user_settings(chat_id)
        min_interval = min(min_interval, settings["interval"])
        all_products.extend(get_user_products(chat_id))
    if not all_products:
        return {
            "healthy": True,
            "status": "healthy",
            "fresh_product_count": 0,
            "stale_product_count": 0,
            "total_product_count": 0,
        }

    stale_after = min_interval * 60 * 3
    current = now_epoch()
    fresh_count = sum(
        1
        for product in all_products
        if product.get("last_success_epoch") is not None
        and (current - int(product.get("last_success_epoch"))) <= stale_after
    )
    stale_count = len(all_products) - fresh_count

    if fresh_count == len(all_products):
        status = "healthy"
    elif fresh_count > 0:
        status = "partial"
    else:
        status = "needs_check"
    return {
        "healthy": status == "healthy",
        "status": status,
        "fresh_product_count": fresh_count,
        "stale_product_count": stale_count,
        "total_product_count": len(all_products),
    }


def scraper_health():
    return scraper_health_summary()["healthy"]


@app.get("/")
def health():
    return public_health_data()


def public_health_data():
    approved = list_approved_users()
    pending = list_pending_users()
    total_products = sum(len(get_user_products(chat_id)) for chat_id in approved)
    health_summary = scraper_health_summary()
    return {
        "status": "running",
        "scraper_healthy": health_summary["healthy"],
        "scraper_status": health_summary["status"],
        "approved_user_count": len(approved),
        "pending_user_count": len(pending),
        "total_product_count": total_products,
        "fresh_product_count": health_summary["fresh_product_count"],
        "stale_product_count": health_summary["stale_product_count"],
        "max_users": MAX_USERS,
        "max_products_per_user": MAX_PRODUCTS_PER_USER,
        "admin_product_cap_exempt": True,
        "max_unique_checks_per_cycle": MAX_UNIQUE_CHECKS_PER_CYCLE,
        "interval_floor_minutes": MIN_CHECK_INTERVAL_MINUTES,
        "check_running": state["check_running"],
    }


@app.get("/dashboard")
def dashboard():
    data = public_health_data()
    scraper_label = {
        "healthy": "Healthy",
        "partial": "Partial",
        "needs_check": "Needs Check",
    }.get(data["scraper_status"], "Needs Check")
    badge_class = "good" if data["scraper_status"] == "healthy" else data["scraper_status"]
    running_label = "Running" if data["status"] == "running" else data["status"].title()
    check_label = "Yes" if data["check_running"] else "No"
    refreshed = now_text()

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stock Tracker Dashboard</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --text: #172033;
      --muted: #687385;
      --line: #dfe5ee;
      --good: #137a3f;
      --partial: #8a6d00;
      --warn: #a15c00;
      --accent: #2157d6;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #101722;
        --panel: #172131;
        --text: #eef3fb;
        --muted: #9ba8ba;
        --line: #2d3a4d;
        --good: #44c07a;
        --partial: #ffd45a;
        --warn: #ffb14a;
        --accent: #83a5ff;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, Helvetica, sans-serif;
    }}
    main {{
      width: min(960px, calc(100% - 32px));
      margin: 0 auto;
      padding: 36px 0;
    }}
    header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.15;
    }}
    .subtle {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--good);
      font-weight: 700;
      white-space: nowrap;
    }}
    .badge.partial {{ color: var(--partial); }}
    .badge.needs_check {{ color: var(--warn); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 20px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-height: 104px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 10px;
    }}
    .metric strong {{
      display: block;
      font-size: 24px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    .details {{
      margin-top: 16px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      padding: 14px 16px;
      border-top: 1px solid var(--line);
    }}
    .row:first-child {{ border-top: 0; }}
    .row span {{ color: var(--muted); }}
    .row strong {{ text-align: right; }}
    a {{ color: var(--accent); }}
    @media (max-width: 760px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 460px) {{
      main {{ width: min(100% - 20px, 960px); padding: 22px 0; }}
      .grid {{ grid-template-columns: 1fr; }}
      .row {{ grid-template-columns: 1fr; gap: 6px; }}
      .row strong {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Stock Tracker Dashboard</h1>
        <p class="subtle">Public service health. Product and user details are hidden.</p>
      </div>
      <div class="badge {badge_class}">{scraper_label}</div>
    </header>

    <section class="grid" aria-label="Service metrics">
      <div class="metric"><span>Service</span><strong>{running_label}</strong></div>
      <div class="metric"><span>Approved Users</span><strong>{data['approved_user_count']}</strong></div>
      <div class="metric"><span>Total Products</span><strong>{data['total_product_count']}</strong></div>
      <div class="metric"><span>Check Running</span><strong>{check_label}</strong></div>
    </section>

    <section class="details" aria-label="Configuration">
      <div class="row"><span>Fresh Products</span><strong>{data['fresh_product_count']}</strong></div>
      <div class="row"><span>Needs Fresh Check</span><strong>{data['stale_product_count']}</strong></div>
      <div class="row"><span>Pending Requests</span><strong>{data['pending_user_count']}</strong></div>
      <div class="row"><span>Minimum Interval</span><strong>{data['interval_floor_minutes']} minutes</strong></div>
      <div class="row"><span>Max Friends</span><strong>{data['max_users']}</strong></div>
      <div class="row"><span>Max Products Per User</span><strong>{data['max_products_per_user']}</strong></div>
      <div class="row"><span>Max Unique Checks Per Cycle</span><strong>{data['max_unique_checks_per_cycle']}</strong></div>
      <div class="row"><span>Admin Product Cap Exempt</span><strong>{'Yes' if data['admin_product_cap_exempt'] else 'No'}</strong></div>
      <div class="row"><span>Last Refreshed</span><strong>{refreshed}</strong></div>
    </section>
  </main>
</body>
</html>"""


if __name__ == "__main__":
    validate_config()
    bootstrap_admin()
    set_bot_commands()
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=run_telegram_controls, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
