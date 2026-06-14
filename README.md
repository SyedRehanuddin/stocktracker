# Amazon Stock Tracker

Tracks the Evofox Katana S Mini Black keyboard on Amazon and sends a Telegram alert when it is available.

## Local Setup

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
.\venv\Scripts\python tracker.py --once
.\venv\Scripts\python tracker.py
```

## Required Environment Variables

```text
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
PRODUCT_URL=https://www.amazon.in/dp/B0G2GMN6Y6
ADDITIONAL_PRODUCT_URLS=https://www.amazon.in/dp/B0CY5HVDS2,https://www.amazon.in/dp/B0CY5QW186
CHECK_INTERVAL_MINUTES=15
REDIS_URL=your_render_key_value_redis_url
```

## Telegram Controls

The bot sends inline buttons with every message:

- Buy on Amazon
- Check Now
- Status
- Add Product
- List Products
- Pause / Resume
- 5m / 10m / 15m / 30m interval
- Notify every check / changes only

Commands also work:

```text
/status
/list
/add
/remove 2
/check
/pause
/resume
/help
```

Links added from Telegram are saved to Redis when `REDIS_URL` is set. Without Redis, local testing saves them to `products.json`.

## Deploy

This project includes a Dockerfile for Render. Deploy it as a Free Web Service, not a Background Worker. Render does not provide Free Background Workers.

Configure the environment variables above in the hosting dashboard. The Dockerfile starts:

```bash
python app.py
```

Render Free Web Services sleep after idle time, so add a free uptime monitor that pings the service URL every 5 minutes.
