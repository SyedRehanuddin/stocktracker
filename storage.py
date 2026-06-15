import json
import os
from pathlib import Path

try:
    import redis
except ImportError:
    redis = None

USERS_KEY = "stock_tracker:users"
PENDING_KEY = "stock_tracker:pending"
REJECTED_KEY = "stock_tracker:rejected"
LOCAL_STORE = Path("multi_user_store.json")

_redis_warning_shown = False
_redis_client = None


def _warn_no_redis(reason):
    global _redis_warning_shown
    if _redis_warning_shown:
        return
    _redis_warning_shown = True
    print(
        "WARNING: Redis is NOT active (" + reason + "). "
        "Falling back to local file storage. On Render free tier this file is "
        "wiped on every restart, so users, products, and settings will NOT persist. "
        "Set a valid REDIS_URL to fix this.",
        flush=True,
    )


def get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client

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
    _redis_client = redis.from_url(redis_url, decode_responses=True)
    return _redis_client


def reset_redis_client():
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.close()
        except Exception:
            pass
    _redis_client = None


def _local_store():
    path = Path(LOCAL_STORE)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"sets": {}, "values": {}}


def _save_local_store(data):
    Path(LOCAL_STORE).write_text(json.dumps(data), encoding="utf-8")


def _json_key(kind, chat_id):
    return f"stock_tracker:user:{chat_id}:{kind}"


def products_key(chat_id):
    return _json_key("products", chat_id)


def settings_key(chat_id):
    return _json_key("settings", chat_id)


def profile_key(chat_id):
    return _json_key("profile", chat_id)


def _load_json(key, default):
    client = get_redis_client()
    if client:
        try:
            raw = client.get(key)
            return json.loads(raw) if raw else default
        except Exception as e:
            print(f"Redis load failed for {key}: {e}", flush=True)
            reset_redis_client()

    return _local_store()["values"].get(key, default)


def _save_json(key, value):
    payload = json.dumps(value)
    client = get_redis_client()
    if client:
        try:
            client.set(key, payload)
            return
        except Exception as e:
            print(f"Redis save failed for {key}: {e}", flush=True)
            reset_redis_client()

    data = _local_store()
    data["values"][key] = value
    _save_local_store(data)


def _set_members(key):
    client = get_redis_client()
    if client:
        try:
            return sorted(client.smembers(key))
        except Exception as e:
            print(f"Redis set read failed for {key}: {e}", flush=True)
            reset_redis_client()

    return sorted(_local_store()["sets"].get(key, []))


def _set_add(key, value):
    value = str(value)
    client = get_redis_client()
    if client:
        try:
            client.sadd(key, value)
            return
        except Exception as e:
            print(f"Redis set add failed for {key}: {e}", flush=True)
            reset_redis_client()

    data = _local_store()
    members = set(data["sets"].get(key, []))
    members.add(value)
    data["sets"][key] = sorted(members)
    _save_local_store(data)


def _set_remove(key, value):
    value = str(value)
    client = get_redis_client()
    if client:
        try:
            client.srem(key, value)
            return
        except Exception as e:
            print(f"Redis set remove failed for {key}: {e}", flush=True)
            reset_redis_client()

    data = _local_store()
    members = set(data["sets"].get(key, []))
    members.discard(value)
    data["sets"][key] = sorted(members)
    _save_local_store(data)


def list_approved_users():
    return _set_members(USERS_KEY)


def list_pending_users():
    return _set_members(PENDING_KEY)


def list_rejected_users():
    return _set_members(REJECTED_KEY)


def is_approved_user(chat_id):
    return str(chat_id) in set(list_approved_users())


def is_pending_user(chat_id):
    return str(chat_id) in set(list_pending_users())


def is_rejected_user(chat_id):
    return str(chat_id) in set(list_rejected_users())


def add_approved_user(chat_id):
    _set_add(USERS_KEY, chat_id)
    _set_remove(PENDING_KEY, chat_id)
    _set_remove(REJECTED_KEY, chat_id)


def add_pending_user(chat_id):
    _set_add(PENDING_KEY, chat_id)


def add_rejected_user(chat_id):
    _set_add(REJECTED_KEY, chat_id)
    _set_remove(PENDING_KEY, chat_id)


def remove_approved_user(chat_id):
    _set_remove(USERS_KEY, chat_id)


def load_user_products(chat_id):
    return _load_json(products_key(chat_id), [])


def save_user_products(chat_id, products):
    _save_json(products_key(chat_id), products)


def load_user_settings(chat_id):
    return _load_json(settings_key(chat_id), {})


def save_user_settings(chat_id, settings):
    _save_json(settings_key(chat_id), settings)


def load_user_profile(chat_id):
    return _load_json(profile_key(chat_id), {})


def save_user_profile(chat_id, profile):
    _save_json(profile_key(chat_id), profile)
