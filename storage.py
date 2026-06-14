import json
import os
from pathlib import Path

try:
    import redis
except ImportError:
    redis = None

PRODUCTS_KEY = "stock_tracker:products"
LOCAL_STORE = Path("products.json")


def get_redis_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url or redis is None:
        return None
    if not redis_url.startswith(("redis://", "rediss://", "unix://")):
        print(
            "Ignoring REDIS_URL because it is not a Redis connection URL. "
            "It must start with redis://, rediss://, or unix://.",
            flush=True,
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
