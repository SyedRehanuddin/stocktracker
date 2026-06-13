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
```

## Deploy

This project includes a Dockerfile for cloud hosts like Render or Railway. Configure the environment variables above in the hosting dashboard, then run:

```bash
python tracker.py
```
