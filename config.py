from dotenv import load_dotenv
import os

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID") or TELEGRAM_CHAT_ID
PRODUCT_URL = os.getenv("PRODUCT_URL")
ADDITIONAL_PRODUCT_URLS = os.getenv("ADDITIONAL_PRODUCT_URLS", "")
MAX_USERS = int(os.getenv("MAX_USERS", "15"))
MAX_PRODUCTS_PER_USER = int(os.getenv("MAX_PRODUCTS_PER_USER", "5"))
MAX_UNIQUE_CHECKS_PER_CYCLE = int(os.getenv("MAX_UNIQUE_CHECKS_PER_CYCLE", "50"))
MIN_CHECK_INTERVAL_MINUTES = int(os.getenv("MIN_CHECK_INTERVAL_MINUTES", "15"))


def validate_config():
    missing = [
        name
        for name, value in {
            "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
            "ADMIN_CHAT_ID": ADMIN_CHAT_ID,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required .env values: {', '.join(missing)}")
