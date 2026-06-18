import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
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
    add_rejected_user,
    is_approved_user,
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
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:smile\.)?(?:amazon\.in|amzn\.in)/\S+|https?://\S+",
    re.IGNORECASE,
)
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
PRODUCT_NAME_LIMIT = 500
SHORT_URL_TIMEOUT = 8
PRODUCT_SEPARATOR = "──────────────────"
NUMBER_EMOJIS = {
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣",
    7: "7️⃣",
    8: "8️⃣",
    9: "9️⃣",
    10: "🔟",
}
SHORT_URL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

state = {
    "check_running": False,
    "check_started_at": None,
    "telegram_offset": None,
    "awaiting_product_url": set(),
    "awaiting_rename": {},
    "pending_product_url": {},
    "pending_remove": {},
}


def short_label(text, limit=42):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    trimmed = text[: limit - 3].rsplit(" ", 1)[0].strip()
    return f"{trimmed}..." if trimmed else text[:limit]


def strip_url(url):
    return url.strip().strip("<>").rstrip(").,")


def ensure_url_scheme(url):
    stripped = strip_url(url)
    if re.match(r"^https?://", stripped, re.IGNORECASE):
        return stripped
    return f"https://{stripped}"


def canonical_amazon_url_from_asin(asin):
    return f"https://www.amazon.in/dp/{asin.upper()}"


def extract_asin_from_path(path):
    parts = [part for part in path.split("/") if part]
    for marker in ("dp", "product"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                candidate = parts[index + 1]
                if ASIN_RE.match(candidate):
                    return candidate.upper()
    return None


def resolve_amzn_short_url(url):
    try:
        response = requests.head(
            url,
            headers=SHORT_URL_HEADERS,
            timeout=SHORT_URL_TIMEOUT,
            allow_redirects=True,
        )
        if response.url and response.url != url:
            return response.url
    except requests.RequestException:
        pass

    response = requests.get(
        url,
        headers=SHORT_URL_HEADERS,
        timeout=SHORT_URL_TIMEOUT,
        allow_redirects=True,
        stream=True,
    )
    response.close()
    return response.url


def clean_url(url, resolve_short=False):
    raw_url = ensure_url_scheme(url)
    parsed = urlparse(raw_url)
    host = parsed.netloc.lower()

    if host.startswith("www."):
        host = host[4:]

    if host == "amzn.in":
        if not resolve_short:
            return raw_url
        resolved_url = resolve_amzn_short_url(raw_url)
        return clean_url(resolved_url, resolve_short=False)

    if host == "smile.amazon.in":
        host = "amazon.in"

    if host not in ("amazon.in", "www.amazon.in"):
        return raw_url

    asin = extract_asin_from_path(parsed.path)
    return canonical_amazon_url_from_asin(asin) if asin else raw_url


def canonicalize_amazon_product_url(url):
    raw_url = ensure_url_scheme(url)
    parsed = urlparse(raw_url)
    host = parsed.netloc.lower()

    if host.startswith("www."):
        host = host[4:]

    if host == "amzn.in":
        try:
            resolved_url = clean_url(raw_url, resolve_short=True)
        except requests.RequestException:
            return None, "Could not resolve Amazon short link. Send the full Amazon product URL."
        if strip_url(resolved_url) == raw_url:
            return None, "Could not resolve Amazon short link. Send the full Amazon product URL."
        canonical_url, error = canonicalize_amazon_product_url(resolved_url)
        if not canonical_url:
            return None, error or "Could not find a product ASIN from that Amazon short link."
        return canonical_url, None

    if host == "smile.amazon.in":
        host = "amazon.in"

    if host != "amazon.in":
        return None, "Please send a valid Amazon India product link."

    if parsed.path in ("/s", "/s/") or parsed.path.startswith("/s/"):
        return None, "Amazon search pages cannot be tracked. Send a product page link."

    asin = extract_asin_from_path(parsed.path)
    if not asin:
        return None, "I could not find an Amazon ASIN in that URL. Send a product page link."

    return canonical_amazon_url_from_asin(asin), None


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
        "button_names": {},
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
    button_names = normalized.get("button_names") or {}
    normalized["button_names"] = {
        str(key): clean_product_name(value)
        for key, value in dict(button_names).items()
        if clean_product_name(value)
    }
    return normalized


def get_user_settings(chat_id):
    settings = normalize_settings(load_user_settings(chat_id))
    save_user_settings(chat_id, settings)
    return settings


def make_product(url, name=None, index=1):
    cleaned_url, _error = canonicalize_amazon_product_url(url)
    cleaned_name = clean_product_name(name) or f"Product {index}"
    return {
        "url": cleaned_url or clean_url(url),
        "name": cleaned_name,
        "source_name": cleaned_name,
        "custom_name": None,
        "last_status": None,
        "last_checked": None,
        "last_success_epoch": None,
        "last_price": None,
        "notified_in_stock": False,
    }


def normalize_product(product, index):
    name = clean_product_name(product.get("name")) or f"Product {index}"
    return {
        "url": clean_url(product["url"]),
        "name": name,
        "source_name": clean_product_name(product.get("source_name")) or name,
        "custom_name": clean_product_name(product.get("custom_name")),
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
    settings = normalize_settings(load_user_settings(chat_id))
    button_names = settings.get("button_names") or {}
    for index, product in enumerate(products, start=1):
        saved_name = clean_product_name(button_names.get(str(index)))
        if saved_name:
            product["custom_name"] = saved_name
    save_user_products(chat_id, products)
    return products


def is_amazon_url(url):
    canonical_url, _error = canonicalize_amazon_product_url(url)
    return canonical_url is not None


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


def product_number_icon(index):
    return NUMBER_EMOJIS.get(index, f"#{index}")


def product_status_block(index, product):
    checked = product["last_checked"] or "never"
    price = product.get("last_price") or "not found"
    return (
        f"*Product {index}*\n"
        f"*Name:* {product_display_name(product)}\n"
        f"*Status:* `{product_status_label(product)}`\n"
        f"*Price:* `{price}`\n"
        f"*Last checked:* `{checked}`"
    )


def controls(settings):
    return {
        "paused": settings["paused"],
        "notify_only_on_change": settings["notify_only_on_change"],
        "interval": settings["interval"],
    }


def inline_keyboard(rows):
    return {"inline_keyboard": rows}


def back_markup(target="back_start"):
    return inline_keyboard([[{"text": "⬅️ Back", "callback_data": target}]])


def send_back_message(message, chat_id, target="back_start"):
    send_telegram_message(message, chat_id=chat_id, reply_markup=back_markup(target))


def product_display_name(product):
    return (
        clean_product_name(product.get("custom_name"))
        or clean_product_name(product.get("source_name"))
        or clean_product_name(product.get("name"))
        or "Product"
    )


def product_button_name(chat_id, index, product):
    custom_name = clean_product_name(product.get("custom_name"))
    if custom_name:
        return short_label(custom_name)

    settings = get_user_settings(chat_id)
    button_name = clean_product_name(settings.get("button_names", {}).get(str(index)))
    if button_name:
        return short_label(button_name)

    return f"Product {index}"


def product_short_name(chat_id, index, product):
    return product_button_name(chat_id, index, product)


def product_limit_text(chat_id):
    return str(len(get_user_products(chat_id)))


def main_menu(chat_id):
    return inline_keyboard(
        [
            [
                {"text": "➕ Add Product", "callback_data": "add"},
                {"text": "📦 My Products", "callback_data": "products_menu"},
            ],
            [
                {"text": "🔍 Check Now", "callback_data": "check"},
                {"text": "📊 Product Status", "callback_data": "status"},
            ],
            [
                {"text": "⚙️ Settings", "callback_data": "settings_menu"},
                {"text": "❓ Help", "callback_data": "help"},
            ],
        ]
    )


def send_main_menu(chat_id):
    send_telegram_message(start_message(chat_id), chat_id=chat_id, reply_markup=main_menu(chat_id))


def product_exists(products, url):
    cleaned, _error = canonicalize_amazon_product_url(url)
    cleaned = cleaned or clean_url(url)
    return any(product["url"] == cleaned for product in products)


def reindex_button_names_after_remove(settings, removed_number):
    button_names = settings.get("button_names") or {}
    reindexed = {}
    for key, value in button_names.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue

        if index < removed_number:
            reindexed[str(index)] = value
        elif index > removed_number:
            reindexed[str(index - 1)] = value
    settings["button_names"] = reindexed


def max_products_for(chat_id):
    return None if is_admin(chat_id) else MAX_PRODUCTS_PER_USER


def add_product(chat_id, url):
    products = get_user_products(chat_id)
    limit = max_products_for(chat_id)
    cleaned_url, error = canonicalize_amazon_product_url(url)
    if not cleaned_url:
        return False, error or "Please send a valid Amazon India product link."
    if product_exists(products, cleaned_url):
        return False, "That product is already being tracked."
    if limit is not None and len(products) >= limit:
        return False, f"Maximum {limit} products allowed. Remove one before adding another."

    products.append(make_product(cleaned_url, index=len(products) + 1))
    save_user_products(chat_id, products)
    return True, f"Product {len(products)}"


def remove_product(chat_id, number_text):
    products = get_user_products(chat_id)
    try:
        number = int(number_text)
    except ValueError:
        return False, "Choose a product from the delete menu."

    if number < 1 or number > len(products):
        return False, "That product number is not in the list."

    removed = products.pop(number - 1)
    save_user_products(chat_id, products)
    settings = get_user_settings(chat_id)
    reindex_button_names_after_remove(settings, number)
    save_user_settings(chat_id, settings)
    return True, f"Removed {product_display_name(removed)}."


def remove_confirmation_message(chat_id, number_text):
    products = get_user_products(chat_id)
    try:
        number = int(number_text)
    except ValueError:
        return "*Invalid product.*"
    if number < 1 or number > len(products):
        return "*Invalid product.*"
    product = products[number - 1]
    return (
        "*Are you sure you want to remove this product?*\n\n"
        "*Product:*\n"
        f"{product_display_name(product)}"
    )


def remove_confirmation_markup(number):
    return inline_keyboard(
        [
            [{"text": "✅ Yes, Remove", "callback_data": f"confirm_remove:{number}"}],
            [{"text": "❌ Cancel", "callback_data": "back_products"}],
            [{"text": "⬅️ Back", "callback_data": "back_products"}],
        ]
    )


def rename_product(chat_id, number_text, new_name):
    products = get_user_products(chat_id)
    try:
        number = int(number_text)
    except ValueError:
        return False, "Use `/rename 2 Gaming Keyboard`."

    if number < 1 or number > len(products):
        return False, "That product number is not in the list."

    cleaned_name = clean_product_name(new_name)
    if not cleaned_name:
        return False, "Send a name after the product number. Example: `/rename 2 Gaming Keyboard`."

    settings = get_user_settings(chat_id)
    button_names = settings.setdefault("button_names", {})
    button_names[str(number)] = cleaned_name
    products[number - 1]["custom_name"] = cleaned_name
    save_user_settings(chat_id, settings)
    save_user_products(chat_id, products)
    return True, rename_success_message(number, cleaned_name)


def rename_success_message(number, cleaned_name):
    return (
        "✅ Product renamed\n\n"
        f"Product {number} → {cleaned_name}\n\n"
        "This product will now appear as:\n"
        f"{cleaned_name}"
    )


def product_list_message(chat_id):
    products = get_user_products(chat_id)
    if not products:
        return "*Tracked Products*\n\nNo products yet. Use `/add` to add one."

    lines = ["*Tracked Products*"]
    for index, product in enumerate(products, start=1):
        lines.append(f"\n{product_status_block(index, product)}")
        lines.append(f"[Buy on Amazon]({product['url']})")
    return "\n".join(lines)


def my_products_message(chat_id):
    products = get_user_products(chat_id)
    lines = ["*📦 My Products*", "", f"*Total:* `{product_limit_text(chat_id)}`"]
    if not products:
        return (
            "You are not tracking any products yet.\n\n"
            "Tap `➕ Add Product` to add your first Amazon product."
        )
    else:
        for index, product in enumerate(products, start=1):
            lines.extend(
                [
                    "",
                    f"{product_number_icon(index)} {product_display_name(product)}",
                ]
            )
    return "\n".join(lines)


def my_products_markup(chat_id=None):
    if chat_id is not None and not get_user_products(chat_id):
        return inline_keyboard(
            [
                [{"text": "➕ Add Product", "callback_data": "add"}],
                [{"text": "⬅️ Back", "callback_data": "back_start"}],
            ]
        )
    return inline_keyboard(
        [
            [{"text": "🔗 View Product Links", "callback_data": "product_links"}],
            [{"text": "✏️ Rename Product", "callback_data": "rename_menu"}],
            [{"text": "🗑 Remove Product", "callback_data": "remove_menu"}],
            [{"text": "⬅️ Back", "callback_data": "back_start"}],
        ]
    )


def send_products_menu(chat_id):
    send_telegram_message(my_products_message(chat_id), chat_id=chat_id, reply_markup=my_products_markup(chat_id))


def product_links_message(chat_id):
    products = get_user_products(chat_id)
    if not products:
        return (
            "*🔗 Product Links*\n\n"
            "You are not tracking any products yet.\n\n"
            "Tap `➕ Add Product` to add your first Amazon product."
        )

    use_dividers = any(len(product_display_name(product)) > 55 for product in products)
    lines = ["*🔗 Product Links*"]
    for index, product in enumerate(products, start=1):
        lines.extend(
            [
                "",
                f"{product_number_icon(index)} {product_display_name(product)}",
                f"🛒 [Buy on Amazon]({product['url']})",
            ]
        )
        if use_dividers and index < len(products):
            lines.append(PRODUCT_SEPARATOR)
    return "\n".join(lines)


def product_links_markup():
    return back_markup("back_products")


def product_rename_rows(chat_id):
    if not get_user_products(chat_id):
        return [
            [{"text": "➕ Add Product", "callback_data": "add"}],
            [{"text": "⬅️ Back", "callback_data": "back_products"}],
        ]

    rows = []
    for index, product in enumerate(get_user_products(chat_id), start=1):
        rows.append(
            [
                {
                    "text": f"✏️ Rename {product_short_name(chat_id, index, product)}",
                    "callback_data": f"rename_product:{index}",
                }
            ]
        )
    rows.append([{"text": "⬅️ Back", "callback_data": "back_products"}])
    return rows


def send_rename_picker(chat_id):
    message = (
        "*Choose product to rename:*\n\n"
        "No products yet. Add a product first."
        if not get_user_products(chat_id)
        else "*Choose product to rename:*"
    )
    send_telegram_message(
        message,
        chat_id=chat_id,
        reply_markup=inline_keyboard(product_rename_rows(chat_id)),
    )


def product_remove_rows(chat_id):
    if not get_user_products(chat_id):
        return [
            [{"text": "➕ Add Product", "callback_data": "add"}],
            [{"text": "⬅️ Back", "callback_data": "back_products"}],
        ]

    rows = []
    for index, product in enumerate(get_user_products(chat_id), start=1):
        rows.append(
            [
                {
                    "text": f"🗑 Remove {product_short_name(chat_id, index, product)}",
                    "callback_data": f"delete_product:{index}",
                }
            ]
        )
    rows.append([{"text": "⬅️ Back", "callback_data": "back_products"}])
    return rows


def remove_picker_message(chat_id):
    if not get_user_products(chat_id):
        return "*Choose product to remove:*\n\nNo products yet. Add a product first."
    return "*Choose product to remove:*"


def send_remove_picker(chat_id):
    send_telegram_message(
        remove_picker_message(chat_id),
        chat_id=chat_id,
        reply_markup=inline_keyboard(product_remove_rows(chat_id)),
    )


def product_summary_lines(chat_id):
    return [
        product_status_block(index, product)
        for index, product in enumerate(get_user_products(chat_id), start=1)
    ]


def status_icon(product):
    status = product_status_label(product)
    if status == "available":
        return "✅ Available"
    if status == "unavailable":
        return "❌ Unavailable"
    if status == "unclear":
        return "⚠️ Unclear"
    return "⚪ Not checked yet"


def status_parts(product):
    status = product_status_label(product)
    if status == "available":
        return "✅", "Available"
    if status == "unavailable":
        return "❌", "Unavailable"
    if status == "unclear":
        return "⚠️", "Unclear"
    return "⚪", "Not checked yet"


def compact_checked_time(product):
    checked = product.get("last_checked")
    if not checked:
        return "Never"
    match = re.search(r"(\d{1,2}:\d{2}\s[AP]M)", checked)
    return match.group(1) if match else checked


def compact_product_status_message(chat_id):
    products = get_user_products(chat_id)
    if not products:
        return (
            "*📊 Product Status*\n\n"
            "No product status available yet.\n\n"
            "Add a product first."
        )

    use_dividers = any(len(product_display_name(product)) > 55 for product in products)
    lines = ["*📊 Product Status*"]
    for index, product in enumerate(products, start=1):
        lines.append(compact_product_status_block(index, product))
        if use_dividers and index < len(products):
            lines.append(PRODUCT_SEPARATOR)
    return "\n".join(lines)


def compact_product_status_block(index, product):
    price = product.get("last_price") or "Not found"
    status_emoji, status_text = status_parts(product)
    return "\n".join(
        [
            "",
            f"{product_number_icon(index)} {product_display_name(product)}",
            "",
            f"{status_emoji} `Status       :` {status_text}",
            f"💰 `Price        :` {price}",
            f"🕒 `Last checked :` {compact_checked_time(product)}",
        ]
    )


def single_product_status_message(chat_id, product):
    products = get_user_products(chat_id)
    index = product_number(products, product) or "?"
    price = product.get("last_price") or "Not found"
    status_emoji, status_text = status_parts(product)
    return "\n".join(
        [
            "✅ *Check Complete*",
            "",
            f"{product_number_icon(index)} {product_display_name(product)}",
            "",
            f"{status_emoji} `Status       :` {status_text}",
            f"💰 `Price        :` {price}",
            f"🕒 `Last checked :` {compact_checked_time(product)}",
            "",
            f"🔗 [Buy on Amazon]({product['url']})",
        ]
    )


def single_check_markup(index):
    return inline_keyboard(
        [
            [{"text": "🔄 Check Again", "callback_data": f"check_product:{index}"}],
            [{"text": "🔍 Check Another", "callback_data": "check"}],
            [{"text": "⬅️ Main Menu", "callback_data": "back_start"}],
        ]
    )


def status_markup():
    return inline_keyboard(
        [
            [{"text": "🔍 Check Now", "callback_data": "check"}],
            [{"text": "⬅️ Back", "callback_data": "back_start"}],
        ]
    )


def empty_state_markup():
    return inline_keyboard(
        [
            [{"text": "➕ Add Product", "callback_data": "add"}],
            [{"text": "⬅️ Back", "callback_data": "back_start"}],
        ]
    )


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


def start_message(chat_id):
    products = get_user_products(chat_id)
    if not products:
        return (
            "*🛒 Amazon Stock Tracker*\n\n"
            "No products added yet.\n\n"
            "Tap `➕ Add Product` and send an Amazon product link to start tracking."
        )

    settings = get_user_settings(chat_id)
    auto_check = "Paused" if settings["paused"] else "On"
    notify_mode = "Stock changes only" if settings["notify_only_on_change"] else "Every check"
    return (
        "*🛒 Amazon Stock Tracker*\n\n"
        f"`Products      :` {len(products)} tracked\n"
        f"`Auto Check    :` {auto_check}\n"
        f"`Check Every   :` {settings['interval']} minutes\n"
        f"`Notifications :` {notify_mode}\n\n"
        "Choose what you want to do below."
    )


def control_status_message(chat_id):
    products = product_summary_lines(chat_id)
    product_text = "\n\n".join(products) if products else "No products yet."

    return (
        "*Tracked Products*\n\n"
        "*Products*\n"
        + product_text
    )


def product_check_rows(chat_id):
    if not get_user_products(chat_id):
        return [
            [{"text": "➕ Add Product", "callback_data": "add"}],
            [{"text": "⬅️ Back", "callback_data": "back_start"}],
        ]

    rows = []
    for index, product in enumerate(get_user_products(chat_id), start=1):
        rows.append(
            [
                {
                    "text": f"🔍 Check {product_button_name(chat_id, index, product)}",
                    "callback_data": f"check_product:{index}",
                }
            ]
        )
    rows.append([{"text": "⬅️ Back", "callback_data": "back_start"}])
    return rows


def check_picker_message(chat_id):
    if not get_user_products(chat_id):
        return (
            "*Choose product to check:*\n\n"
            "No products to check yet.\n\n"
            "Add a product first."
        )
    return "*Choose product to check:*"


def send_check_picker(chat_id):
    send_telegram_message(
        check_picker_message(chat_id),
        chat_id=chat_id,
        reply_markup={"inline_keyboard": product_check_rows(chat_id)},
    )


def product_delete_rows(chat_id):
    rows = []
    for index, product in enumerate(get_user_products(chat_id), start=1):
        rows.append(
            [
                {
                    "text": f"🗑 Remove {product_button_name(chat_id, index, product)}",
                    "callback_data": f"delete_product:{index}",
                }
            ]
        )
    rows.append([{"text": "⬅️ Back", "callback_data": "back_products"}])
    return rows


def delete_picker_message(chat_id):
    return remove_picker_message(chat_id)


def send_delete_picker(chat_id):
    send_telegram_message(
        delete_picker_message(chat_id),
        chat_id=chat_id,
        reply_markup={"inline_keyboard": product_delete_rows(chat_id)},
    )


def interval_menu_message(settings):
    return (
        "*⏱ Check Every*\n\n"
        f"*Current:* `{settings['interval']} minutes`\n\n"
        "Choose how often I should check your products."
    )


def interval_menu_markup(settings):
    def text(minutes):
        return f"✅ Every {minutes} minutes" if settings["interval"] == minutes else f"Every {minutes} minutes"

    return {
        "inline_keyboard": [
            [{"text": text(15), "callback_data": "interval:15"}],
            [{"text": text(30), "callback_data": "interval:30"}],
            [{"text": text(60), "callback_data": "interval:60"}],
            [{"text": "⬅️ Back", "callback_data": "back_settings"}],
        ]
    }


def send_interval_menu(chat_id):
    settings = get_user_settings(chat_id)
    send_telegram_message(
        interval_menu_message(settings),
        chat_id=chat_id,
        reply_markup=interval_menu_markup(settings),
    )


def alert_menu_message(settings):
    mode = "Only when stock changes" if settings["notify_only_on_change"] else "Every check"
    return (
        "*🔔 Notifications*\n\n"
        f"*Current:* `{mode}`\n\n"
        "Choose when I should notify you."
    )


def alert_menu_markup(settings):
    every_text = "✅ Notify every check" if not settings["notify_only_on_change"] else "Notify every check"
    changes_text = "Notify only when stock changes" if not settings["notify_only_on_change"] else "✅ Notify only when stock changes"
    return {
        "inline_keyboard": [
            [{"text": every_text, "callback_data": "notify:every"}],
            [{"text": changes_text, "callback_data": "notify:changes"}],
            [{"text": "⬅️ Back", "callback_data": "back_settings"}],
        ]
    }


def send_alert_menu(chat_id):
    settings = get_user_settings(chat_id)
    send_telegram_message(
        alert_menu_message(settings),
        chat_id=chat_id,
        reply_markup=alert_menu_markup(settings),
    )


def settings_message(settings):
    auto_check = "Paused" if settings["paused"] else "On"
    alert_mode = "Stock changes only" if settings["notify_only_on_change"] else "Every check"
    return (
        "*⚙️ Settings*\n\n"
        f"`Auto Check    :` {auto_check}\n"
        f"`Check Every   :` {settings['interval']} minutes\n"
        f"`Notifications :` {alert_mode}\n\n"
        "Change your tracker settings below."
    )


def settings_markup(settings):
    toggle = (
        {"text": "▶️ Resume Auto Checks", "callback_data": "resume"}
        if settings["paused"]
        else {"text": "⏸ Pause Auto Checks", "callback_data": "pause"}
    )
    return inline_keyboard(
        [
            [{"text": "⏱ Check Every...", "callback_data": "interval_menu"}],
            [{"text": "🔔 Notifications", "callback_data": "alert_menu"}],
            [toggle],
            [{"text": "⬅️ Back", "callback_data": "back_start"}],
        ]
    )


def send_settings_menu(chat_id):
    settings = get_user_settings(chat_id)
    send_telegram_message(settings_message(settings), chat_id=chat_id, reply_markup=settings_markup(settings))


def help_message(chat_id):
    admin_lines = (
        "\n\n*Admin commands:*\n\n"
        "```text\n"
        "/users          - list users\n"
        "/removeuser 123 - remove user access\n"
        "```"
        if is_admin(chat_id)
        else ""
    )
    return (
        "*📘 Help*\n\n"
        "This bot tracks Amazon India product stock and price updates.\n\n"
        "*How to use:*\n"
        "\n"
        "1️⃣ Tap `➕ Add Product`\n"
        "Send an Amazon product link.\n\n"
        "2️⃣ Tap `📦 My Products`\n"
        "View product links, rename products, or remove products.\n\n"
        "3️⃣ Tap `🔍 Check Now`\n"
        "Manually check stock and price.\n\n"
        "4️⃣ Tap `📊 Product Status`\n"
        "See stock status, current price, and last checked time.\n\n"
        "5️⃣ Tap `⚙️ Settings`\n"
        "Change Auto Check timing, Notifications, and pause/resume automatic checks.\n\n"
        "*Commands:*\n"
        "\n"
        "```text\n"
        "/start      - open main menu\n"
        "/check      - choose product to check\n"
        "/add        - add product\n"
        "/status     - show stock status and price\n"
        "/list       - show product links\n"
        "/rename     - choose product to rename\n"
        "/remove     - choose product to remove\n"
        "/pause      - pause auto checks\n"
        "/resume     - resume auto checks\n"
        "/help       - show this help\n"
        "```"
        f"{admin_lines}"
    )


def send_help_menu(chat_id):
    send_telegram_message(help_message(chat_id), chat_id=chat_id, reply_markup=back_markup("back_start"))


def product_number(products, product):
    for index, tracked_product in enumerate(products, start=1):
        if tracked_product["url"] == product["url"]:
            return index
    return None


def should_send_status(settings, product, available, previous_status):
    if settings["notify_only_on_change"]:
        return previous_status != available
    return True


def apply_product_result(
    chat_id,
    products,
    product,
    result,
    force_notify=False,
    send_alert=True,
):
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
    is_first_check = not product.get("last_checked")
    if title:
        product["source_name"] = title
        product["name"] = title
    product["last_price"] = price
    product["last_status"] = available
    product["last_checked"] = now_text()
    if available is True or available is False:
        product["last_success_epoch"] = now_epoch()

    send_now = send_alert and not is_first_check and (
        force_notify or should_send_status(settings, product, available, previous_status)
    )
    if send_now:
        index = product_number(products, product)
        notification_name = (
            f"{product_number_icon(index)} {product_display_name(product)}" if index else product_display_name(product)
        )
        send_status_alert(
            available,
            product_name=notification_name,
            product_url=product["url"],
            price=product.get("last_price"),
            checked_time=compact_checked_time(product),
            check_callback=f"check_product:{index}" if index else "check",
            previous_status=previous_status,
            chat_id=chat_id,
            **controls(settings),
        )

    if available is True:
        product["notified_in_stock"] = True
    elif available is False:
        product["notified_in_stock"] = False
    return available


def run_single_product_check(chat_id, product_index, force_notify=False, send_alert=True):
    products = get_user_products(chat_id)
    if product_index < 0 or product_index >= len(products):
        return None

    product = products[product_index]
    print(f"Checking {chat_id} {product_display_name(product)}: {product['url']}", flush=True)
    results = check_urls([product["url"]])
    result = results[0] if results else {"available": None, "title": None}
    apply_product_result(
        chat_id,
        products,
        product,
        result,
        force_notify=force_notify,
        send_alert=send_alert,
    )
    save_user_products(chat_id, products)
    return product


def single_check_summary_message(chat_id, product):
    return single_product_status_message(chat_id, product)


def run_single_product_check_async(chat_id, product_index):
    settings = get_user_settings(chat_id)
    if not begin_check():
        send_back_message("*Check Already Running.*", chat_id=chat_id)
        return

    products = get_user_products(chat_id)
    if product_index < 0 or product_index >= len(products):
        finish_check()
        send_back_message("*Invalid Product Number.*\n\nThat product number is not in the list.", chat_id=chat_id)
        return

    send_telegram_message(
        f"*🔍 Check started*\n\n{product_number_icon(product_index + 1)} {product_display_name(products[product_index])}",
        chat_id=chat_id,
        reply_markup={"inline_keyboard": []},
    )

    def worker():
        try:
            checked_product = run_single_product_check(
                chat_id,
                product_index,
                force_notify=False,
                send_alert=False,
            )
            if checked_product:
                checked_products = get_user_products(chat_id)
                checked_number = product_number(checked_products, checked_product) or product_index + 1
                send_telegram_message(
                    single_check_summary_message(chat_id, checked_product),
                    chat_id=chat_id,
                    reply_markup=single_check_markup(checked_number),
                )
        except Exception as e:
            print(f"Manual check failed: {e}", flush=True)
            send_back_message(f"*Check Failed:* `{e}`", chat_id=chat_id)
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


def profile_from_message(message, status="approved"):
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


def request_access(message):
    chat_id = chat_id_from_message(message)
    if is_approved_user(chat_id):
        send_main_menu(chat_id)
        return
    if is_rejected_user(chat_id):
        send_telegram_message(
            "*Access removed.*\n\nContact the admin if this is a mistake.",
            chat_id=chat_id,
            reply_markup={"inline_keyboard": []},
        )
        return
    if approved_friend_count() >= MAX_USERS:
        send_telegram_message(
            "*Access Is Full Right Now.*\n\nPlease try again later.",
            chat_id=chat_id,
            reply_markup={"inline_keyboard": []},
        )
        profile = profile_from_message(message, status="blocked_full")
        save_user_profile(chat_id, profile)
        username = f"@{profile['username']}" if profile.get("username") else "none"
        send_telegram_message(
            "*New User Blocked: Limit Full*\n\n"
            f"*Name:* `{display_name(profile)}`\n"
            f"*Username:* `{username}`\n"
            f"*Chat ID:* `{chat_id}`\n"
            f"*Limit:* `{approved_friend_count()}/{MAX_USERS}`",
            chat_id=ADMIN_CHAT_ID,
            reply_markup={"inline_keyboard": []},
        )
        return

    profile = profile_from_message(message, status="approved")
    profile.update(
        {
            "approved_at": now_text(),
            "approved_by": "auto",
        }
    )
    save_user_profile(chat_id, profile)
    add_approved_user(chat_id)
    if not load_user_settings(chat_id):
        save_user_settings(chat_id, default_settings())
    if load_user_products(chat_id) is None:
        save_user_products(chat_id, [])

    username = f"@{profile['username']}" if profile.get("username") else "none"
    send_telegram_message(
        "👤 *New user started the bot*\n\n"
        f"*Name:* `{display_name(profile)}`\n"
        f"*Username:* `{username}`\n"
        f"*Chat ID:* `{chat_id}`\n"
        "*Status:* `approved automatically`\n"
        f"*Users:* `{approved_friend_count()}/{MAX_USERS}`",
        chat_id=ADMIN_CHAT_ID,
        reply_markup={"inline_keyboard": []},
    )
    send_main_menu(chat_id)


def users_message():
    approved = list_approved_users()
    pending = list_pending_users()
    rejected = list_rejected_users()
    lines = ["*Users*"]
    lines.append(f"\n*Approved:* `{len(approved)}`")
    for chat_id in approved:
        profile = load_user_profile(chat_id)
        label = "admin" if is_admin(chat_id) else "user"
        lines.append(f"- `{chat_id}` ({label}) {display_name(profile)}")
    lines.append(f"\n*Pending:* `{len(pending)}`")
    for chat_id in pending:
        profile = load_user_profile(chat_id)
        lines.append(f"- `{chat_id}` {display_name(profile)}")
    lines.append(f"\n*Removed/Blocked:* `{len(rejected)}`")
    for chat_id in rejected:
        profile = load_user_profile(chat_id)
        lines.append(f"- `{chat_id}` {display_name(profile)}")
    return "\n".join(lines)


def ensure_authorized(chat_id):
    return is_approved_user(chat_id)


def prompt_for_url(chat_id):
    state["awaiting_product_url"].add(str(chat_id))
    send_back_message(
        "Send me an Amazon product link.\n\n"
        "*Supported examples:*\n"
        "`amazon.in/dp/...`\n"
        "`amazon.in/gp/product/...`\n"
        "`amzn.in/d/...`",
        chat_id=chat_id,
    )


def add_success_markup():
    return inline_keyboard(
        [
            [{"text": "🔍 Check Now", "callback_data": "check"}],
            [{"text": "➕ Add Another Product", "callback_data": "add"}],
            [{"text": "⬅️ Back", "callback_data": "back_start"}],
        ]
    )


def handle_product_url(chat_id, text):
    match = URL_RE.search(text)
    if not match:
        send_back_message("*URL Not Found.*\n\nSend the Amazon product link.", chat_id=chat_id)
        return

    ok, message = add_product(chat_id, match.group(0))
    state["awaiting_product_url"].discard(str(chat_id))
    if ok:
        send_telegram_message(
            "*✅ Product added successfully.*\n\n"
            f"*Saved as:* `{message}`\n\n"
            "*Tip:*\n"
            "You can rename the product button from 📦 My Products → ✏️ Rename Product.",
            chat_id=chat_id,
            reply_markup=add_success_markup(),
        )
    else:
        send_back_message(f"*{message}*", chat_id=chat_id)


def direct_url_confirmation_markup():
    return inline_keyboard(
        [
            [{"text": "✅ Add Product", "callback_data": "confirm_add_url"}],
            [{"text": "❌ Cancel", "callback_data": "cancel_add_url"}],
        ]
    )


def prompt_direct_url_confirmation(chat_id, url):
    cleaned_url, error = canonicalize_amazon_product_url(url)
    if not cleaned_url:
        send_back_message(f"*{error or 'Please send a valid Amazon India product link.'}*", chat_id=chat_id)
        return
    state["pending_product_url"][str(chat_id)] = cleaned_url
    send_telegram_message(
        "*Amazon product link detected.*\n\n"
        "Do you want to add this product to your tracker?",
        chat_id=chat_id,
        reply_markup=direct_url_confirmation_markup(),
    )


def prompt_for_rename(chat_id, product_number):
    state["awaiting_rename"][str(chat_id)] = product_number
    send_back_message(
        "Send the new short name for this product.",
        chat_id=chat_id,
        target="back_products",
    )


def handle_rename_text(chat_id, text):
    product_number = state["awaiting_rename"].pop(str(chat_id), None)
    if not product_number:
        return False
    ok, message = rename_product(chat_id, str(product_number), text)
    send_back_message(message if ok else f"*{message}*", chat_id=chat_id, target="back_products")
    return True


def handle_command(message):
    text = message.get("text", "")
    parts = text.split()
    command = parts[0].lower()
    chat_id = chat_id_from_message(message)

    if command == "/start":
        if is_admin(chat_id):
            bootstrap_admin()
        if ensure_authorized(chat_id):
            send_main_menu(chat_id)
        else:
            request_access(message)
        return

    if not ensure_authorized(chat_id):
        send_telegram_message("*Access Not Approved Yet.*\n\nSend /start to request access.", chat_id=chat_id, reply_markup={"inline_keyboard": []})
        return

    settings = get_user_settings(chat_id)
    if command == "/status":
        markup = empty_state_markup() if not get_user_products(chat_id) else status_markup()
        send_telegram_message(compact_product_status_message(chat_id), chat_id=chat_id, reply_markup=markup)
    elif command == "/list":
        send_telegram_message(product_links_message(chat_id), chat_id=chat_id, reply_markup=product_links_markup())
    elif command == "/add":
        if len(parts) > 1:
            handle_product_url(chat_id, " ".join(parts[1:]))
        else:
            prompt_for_url(chat_id)
    elif command == "/rename":
        send_rename_picker(chat_id)
    elif command == "/check":
        send_check_picker(chat_id)
    elif command == "/remove":
        send_remove_picker(chat_id)
    elif command == "/delete":
        send_remove_picker(chat_id)
    elif command == "/pause":
        settings["paused"] = True
        save_user_settings(chat_id, settings)
        send_telegram_message(
            "*⏸ Auto checks paused.*\n\n"
            "Your products are saved, but scheduled checks are stopped.",
            chat_id=chat_id,
            reply_markup=inline_keyboard(
                [
                    [{"text": "▶️ Resume Auto Checks", "callback_data": "resume"}],
                    [{"text": "⚙️ Settings", "callback_data": "settings_menu"}],
                    [{"text": "⬅️ Back", "callback_data": "back_start"}],
                ]
            ),
        )
    elif command == "/resume":
        settings["paused"] = False
        save_user_settings(chat_id, settings)
        send_telegram_message(
            "*▶️ Auto checks resumed.*\n\n"
            "Scheduled checks are active again.",
            chat_id=chat_id,
            reply_markup=inline_keyboard(
                [
                    [{"text": "⚙️ Settings", "callback_data": "settings_menu"}],
                    [{"text": "⬅️ Back", "callback_data": "back_start"}],
                ]
            ),
        )
    elif command == "/users" and is_admin(chat_id):
        send_telegram_message(users_message(), chat_id=chat_id)
    elif command == "/removeuser" and is_admin(chat_id):
        if len(parts) < 2:
            send_telegram_message("*Usage:* `/removeuser 123456789`.", chat_id=chat_id)
        else:
            remove_approved_user(parts[1])
            add_rejected_user(parts[1])
            profile = load_user_profile(parts[1]) or {"chat_id": parts[1]}
            profile["status"] = "removed"
            profile["removed_at"] = now_text()
            save_user_profile(parts[1], profile)
            send_telegram_message(f"*Removed User Access:* `{parts[1]}`", chat_id=chat_id)
    elif command in ("/users", "/removeuser"):
        send_telegram_message("*Admin command only.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
    elif command == "/help":
        send_help_menu(chat_id)
    elif command == "/cancel":
        state["awaiting_rename"].pop(str(chat_id), None)
        state["awaiting_product_url"].discard(str(chat_id))
        state["pending_product_url"].pop(str(chat_id), None)
        state["pending_remove"].pop(str(chat_id), None)
        send_main_menu(chat_id)


def handle_callback(query):
    data = query.get("data", "")
    callback_id = query.get("id")
    message = query.get("message", {})
    chat_id = chat_id_from_message(message)

    if not ensure_authorized(chat_id):
        answer_callback_query(callback_id, "Access not approved")
        send_telegram_message("*Access not approved yet.*", chat_id=chat_id, reply_markup={"inline_keyboard": []})
        return

    settings = get_user_settings(chat_id)
    if data == "check":
        answer_callback_query(callback_id, "Choose a product")
        send_check_picker(chat_id)
    elif data == "back_start":
        state["awaiting_rename"].pop(str(chat_id), None)
        state["awaiting_product_url"].discard(str(chat_id))
        state["pending_product_url"].pop(str(chat_id), None)
        state["pending_remove"].pop(str(chat_id), None)
        answer_callback_query(callback_id, "Back")
        send_main_menu(chat_id)
    elif data == "back_products":
        state["awaiting_rename"].pop(str(chat_id), None)
        state["awaiting_product_url"].discard(str(chat_id))
        state["pending_product_url"].pop(str(chat_id), None)
        state["pending_remove"].pop(str(chat_id), None)
        answer_callback_query(callback_id, "Back")
        send_products_menu(chat_id)
    elif data == "back_settings":
        answer_callback_query(callback_id, "Back")
        send_settings_menu(chat_id)
    elif data == "products_menu":
        answer_callback_query(callback_id, "My products")
        send_products_menu(chat_id)
    elif data == "product_links":
        answer_callback_query(callback_id, "Product links")
        send_telegram_message(product_links_message(chat_id), chat_id=chat_id, reply_markup=product_links_markup())
    elif data == "rename_menu":
        answer_callback_query(callback_id, "Choose product to rename")
        send_rename_picker(chat_id)
    elif data == "remove_menu" or data == "delete":
        answer_callback_query(callback_id, "Choose product to remove")
        send_remove_picker(chat_id)
    elif data == "settings_menu":
        answer_callback_query(callback_id, "Settings")
        send_settings_menu(chat_id)
    elif data == "help":
        answer_callback_query(callback_id, "Help")
        send_help_menu(chat_id)
    elif data.startswith("check_product:"):
        product_number = int(data.split(":", 1)[1])
        answer_callback_query(callback_id, f"Checking Product {product_number}")
        run_single_product_check_async(chat_id, product_number - 1)
    elif data.startswith("rename_product:"):
        product_number = int(data.split(":", 1)[1])
        answer_callback_query(callback_id, f"Rename Product {product_number}")
        prompt_for_rename(chat_id, product_number)
    elif data.startswith("delete_product:"):
        product_number = data.split(":", 1)[1]
        state["pending_remove"][str(chat_id)] = product_number
        answer_callback_query(callback_id, "Confirm remove")
        send_telegram_message(
            remove_confirmation_message(chat_id, product_number),
            chat_id=chat_id,
            reply_markup=remove_confirmation_markup(product_number),
        )
    elif data.startswith("confirm_remove:"):
        product_number = data.split(":", 1)[1]
        pending_number = state["pending_remove"].pop(str(chat_id), None)
        if pending_number != product_number:
            answer_callback_query(callback_id, "Remove expired")
            send_remove_picker(chat_id)
            return
        ok, message = remove_product(chat_id, product_number)
        answer_callback_query(callback_id, "Removed" if ok else "Not removed")
        if ok:
            send_back_message("*✅ Product removed successfully.*", chat_id=chat_id, target="back_products")
        else:
            send_back_message(f"*{message}*", chat_id=chat_id, target="back_products")
    elif data == "status":
        answer_callback_query(callback_id, "Sending status")
        markup = empty_state_markup() if not get_user_products(chat_id) else status_markup()
        send_telegram_message(compact_product_status_message(chat_id), chat_id=chat_id, reply_markup=markup)
    elif data == "add":
        answer_callback_query(callback_id, "Send a product link")
        prompt_for_url(chat_id)
    elif data == "confirm_add_url":
        pending_url = state["pending_product_url"].pop(str(chat_id), None)
        answer_callback_query(callback_id, "Adding product")
        if pending_url:
            handle_product_url(chat_id, pending_url)
        else:
            send_back_message("*No pending product link found.*", chat_id=chat_id)
    elif data == "cancel_add_url":
        state["pending_product_url"].pop(str(chat_id), None)
        answer_callback_query(callback_id, "Canceled")
        send_main_menu(chat_id)
    elif data == "list":
        answer_callback_query(callback_id, "Sending product list")
        send_products_menu(chat_id)
    elif data == "pause":
        settings["paused"] = True
        save_user_settings(chat_id, settings)
        answer_callback_query(callback_id, "Paused")
        send_telegram_message(
            "*⏸ Auto checks paused.*\n\n"
            "Your products are saved, but scheduled checks are stopped.",
            chat_id=chat_id,
            reply_markup=inline_keyboard(
                [
                    [{"text": "▶️ Resume Auto Checks", "callback_data": "resume"}],
                    [{"text": "⚙️ Settings", "callback_data": "settings_menu"}],
                    [{"text": "⬅️ Back", "callback_data": "back_start"}],
                ]
            ),
        )
    elif data == "resume":
        settings["paused"] = False
        save_user_settings(chat_id, settings)
        answer_callback_query(callback_id, "Resumed")
        send_telegram_message(
            "*▶️ Auto checks resumed.*\n\n"
            "Scheduled checks are active again.",
            chat_id=chat_id,
            reply_markup=inline_keyboard(
                [
                    [{"text": "⚙️ Settings", "callback_data": "settings_menu"}],
                    [{"text": "⬅️ Back", "callback_data": "back_start"}],
                ]
            ),
        )
    elif data == "interval_menu":
        answer_callback_query(callback_id, "Choose interval")
        send_interval_menu(chat_id)
    elif data == "alert_menu":
        answer_callback_query(callback_id, "Choose notifications")
        send_alert_menu(chat_id)
    elif data.startswith("interval:"):
        settings["interval"] = max(int(data.split(":", 1)[1]), MIN_CHECK_INTERVAL_MINUTES)
        save_user_settings(chat_id, settings)
        answer_callback_query(callback_id, f"Interval set to {settings['interval']}m")
        send_telegram_message(
            "*✅ Check interval updated.*\n\n"
            f"I will check your products every `{settings['interval']} minutes`.",
            chat_id=chat_id,
            reply_markup=inline_keyboard(
                [
                    [{"text": "⚙️ Settings", "callback_data": "settings_menu"}],
                    [{"text": "⬅️ Back", "callback_data": "back_settings"}],
                ]
            ),
        )
    elif data in ("toggle_notify", "notify:every", "notify:changes"):
        if data == "notify:every":
            settings["notify_only_on_change"] = False
        elif data == "notify:changes":
            settings["notify_only_on_change"] = True
        else:
            settings["notify_only_on_change"] = not settings["notify_only_on_change"]
        save_user_settings(chat_id, settings)
        mode = "Stock changes only" if settings["notify_only_on_change"] else "Every check"
        answer_callback_query(callback_id, f"Notifications: {mode}")
        send_telegram_message(
            "*✅ Notifications updated.*\n\n"
            f"*Current:* `{mode}`",
            chat_id=chat_id,
            reply_markup=inline_keyboard(
                [
                    [{"text": "⚙️ Settings", "callback_data": "settings_menu"}],
                    [{"text": "⬅️ Back", "callback_data": "back_settings"}],
                ]
            ),
        )


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
                    elif ensure_authorized(chat_id) and str(chat_id) in state["awaiting_rename"]:
                        handle_rename_text(chat_id, text)
                    elif ensure_authorized(chat_id) and str(chat_id) in state["awaiting_product_url"]:
                        handle_product_url(chat_id, text)
                    elif ensure_authorized(chat_id) and URL_RE.search(text):
                        prompt_direct_url_confirmation(chat_id, URL_RE.search(text).group(0))
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
