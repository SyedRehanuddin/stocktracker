import json
import os
from pathlib import Path

try:
    import redis
except ImportError:
    redis = None

PRODUCTS_KEY = "stock_tracker:products"
SETTINGS_KEY = "stock_tracker:settings"
LOCAL_STORE = Path("products.json")
LOCAL_SETTINGS_STORE = Path("settings.json")

# Show the "no Redis" warning only once so logs are not spammed.
_redis_warning_shown = False


def _warn_no_redis(reason):
    global _redis_warning_shown
    if _redis_warning_shown:
        return
    _redis_warning_shown = True
    print(
        "WARNING: Redis is NOT active (" + reason + "). "
        "Falling back to local file storage. On Render free tier this file is "
        "wiped on every restart, so products and settings will NOT persist. "
        "Set a valid REDIS_URL to fix this.",
        flush=True,
    )


def get_redis_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        _warn_no_redis("REDIS_URL is not set")
        return None
    if redis is None:
        _warn_no_redis("the 'redis' package is not installed")
        return None
    if not redis_url.startswith(("redis://", "rediss://", "unix://")):
        _warn_no_redis(
            "REDIS_URL is not a Redis connection URL "
            "(must start with redis://, rediss://, or unix://)"
        )
        return None
    return redis.from_url(redis_url, decode_responses=True)


def load_products():
    client = get_redis_client()
    if client:
        try:
            raw = client.get(PRODUCTS_KEY)
            return json.loads(raw) if raw else []
        except Exception as e:
            print(f"Redis load failed: {e}", flush=True)

    if LOCAL_STORE.exists():
        return json.loads(LOCAL_STORE.read_text(encoding="utf-8"))

    return []


def save_products(products):
    payload = json.dumps(products)
    client = get_redis_client()
    if client:
        try:
            client.set(PRODUCTS_KEY, payload)
            return
        except Exception as e:
            print(f"Redis save failed: {e}", flush=True)

    LOCAL_STORE.write_text(payload, encoding="utf-8")


def load_settings():
    client = get_redis_client()
    if client:
        try:
            raw = client.get(SETTINGS_KEY)
            return json.loads(raw) if raw else {}
        except Exception as e:
            print(f"Redis settings load failed: {e}", flush=True)

    if LOCAL_SETTINGS_STORE.exists():
        return json.loads(LOCAL_SETTINGS_STORE.read_text(encoding="utf-8"))

    return {}


def save_settings(settings):
    payload = json.dumps(settings)
    client = get_redis_client()
    if client:
        try:
            client.set(SETTINGS_KEY, payload)
            return
        except Exception as e:
            print(f"Redis settings save failed: {e}", flush=True)

    LOCAL_SETTINGS_STORE.write_text(payload, encoding="utf-8")
