# Amazon Stock Tracker

A Python service that tracks availability of multiple products on Amazon India
and sends a Telegram alert when an out-of-stock product becomes available.
Products are added, removed, and managed entirely from Telegram. State persists
in Redis.

## How It Runs

`app.py` is the actual service. It runs three things in one process:

- A Flask web server (used for the health endpoint and to stay awake on Render).
- The Telegram bot (commands and inline buttons).
- A background scheduler that checks all tracked products on an interval.

`tracker.py --once` is a legacy single-product command-line check. It is useful
only as a quick connectivity smoke test (does scraping + Telegram work at all).
It does NOT start the bot or multi-product tracking. Use `app.py` for that.

## Local Setup

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt

# Quick connectivity test (single product, no bot):
.\venv\Scripts\python tracker.py --once

# Run the full service (bot + scheduler + web server):
.\venv\Scripts\python app.py
```

Without a valid `REDIS_URL`, storage falls back to local `products.json` and
`settings.json`, and the app prints a warning. That fallback is fine for local
testing but does NOT persist on Render free tier, which wipes the filesystem on
every restart.

## Environment Variables

```text
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
PRODUCT_URL=https://www.amazon.in/dp/B0G2GMN6Y6
ADDITIONAL_PRODUCT_URLS=https://www.amazon.in/dp/B0CY5HVDS2,https://www.amazon.in/dp/B0CY5QW186,https://www.amazon.in/dp/B0B7CMZ3QH
CHECK_INTERVAL_MINUTES=15
REDIS_URL=your_render_key_value_redis_url
PROXY_URL=optional_proxy_url   # leave unset unless Amazon starts blocking
```

`PRODUCT_URL` and `ADDITIONAL_PRODUCT_URLS` seed the tracker ONLY on first run,
when storage is empty. After that, Redis is the source of truth, so products you
delete from the Telegram delete menu stay deleted across restarts and redeploys.

## Telegram Controls

The bot sends inline buttons with every message:

- Add Product
- My Products
- Check Now
- Product Status
- Settings
- Help

Commands also work:

```text
/start
/add
/check
/status
/list
/rename
/remove
/pause
/resume
/help
```

## Health Endpoint

`GET /` returns JSON with the full tracker state, including a `scraper_healthy`
flag. It is `true` only when every tracked product has produced a clear
(available or unavailable) result within the last few check cycles. If the
scraper goes blind (Amazon blocking, page-structure change, repeated errors),
`scraper_healthy` turns `false` even though the app is still "running". Right
after a deploy it reads `false` until the first full check cycle completes; that
is expected.

## Deploy

This project includes a Dockerfile for Render. Deploy it as a Free Web Service,
not a Background Worker. Render does not provide Free Background Workers.

Configure the environment variables above in the hosting dashboard. The
Dockerfile starts:

```bash
python app.py
```

Render Free Web Services sleep after idle time, so add a free uptime monitor
that pings the service URL every 5 minutes.
