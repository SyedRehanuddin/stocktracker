# Amazon Product Availability Tracker

## What This Project Does

This is a Telegram-controlled stock tracker for Amazon India products.

Users add Amazon product links to the bot. The Render service checks product pages on a schedule and sends Telegram alerts when products are available, unavailable, or unclear depending on the user's notification setting.

The project is built as a portfolio-style personal tool, not a public SaaS product.

## Main Features

- Tracks Amazon India product availability.
- Sends Telegram notifications.
- Supports multiple approved users.
- Each user has their own product list.
- Each user has their own settings.
- Admin approves or rejects users.
- Products can be added from Telegram without changing Render environment variables.
- Product action buttons can be renamed with `/rename`.
- Products can be deleted from an inline button menu.
- Public health endpoint and dashboard exist without exposing private product/user details.

## Platforms Used

- GitHub: source code repository and Render auto-deploy trigger.
- Render: hosts the Flask web service on the free web service plan.
- Redis on Render: stores users, products, settings, and renamed button labels.
- Telegram Bot API: bot commands, inline buttons, and alerts.
- UptimeRobot: keeps the Render free web service awake by pinging the health endpoint.

## Why Render Web Service

Render free tier does not provide free background workers, so the tracker runs as a Flask web service.

The Flask app exposes:

- `/` public JSON health endpoint
- `/dashboard` public HTML dashboard

The scheduler and Telegram polling run inside background threads in the same web service.

## Why Redis

Redis is used so data survives redeploys and Render restarts.

It stores:

- approved users
- pending users
- rejected users
- each user's products
- each user's settings
- each user's profile
- renamed product button labels

## Redis Data Structure

```text
stock_tracker:users
stock_tracker:pending
stock_tracker:rejected
stock_tracker:user:{chat_id}:products
stock_tracker:user:{chat_id}:settings
stock_tracker:user:{chat_id}:profile
```

## Multi-User System

Anyone can send `/start`, but they do not get access automatically.

Flow:

1. New user sends `/start`.
2. Admin receives an approval request with name, username, and chat ID.
3. Admin taps Approve or Reject.
4. Approved users get their own empty tracker.
5. Rejected users stay rejected and do not spam new approval requests.

Limits:

- Max 10 approved friends plus admin.
- Max 5 products per normal user.
- Admin is exempt from the product cap.
- Minimum check interval is 15 minutes.
- Max 50 unique product URLs checked per scheduled cycle.

## Telegram Main Menu Buttons

- 🔍 Check Products
- 📋 Tracker Status
- ➕ Add Amazon Product
- 🗑 Delete Amazon Product
- 📦 My Products
- ⏸ Pause Tracking
- ▶️ Resume Tracking
- ⏱ How often to check
- 🔔 Alert settings

## Telegram Commands

```text
/start
/status
/list
/add
/rename 2 Gaming Keyboard
/check
/delete
/cancel
/pause
/resume
/help
```

Admin-only commands:

```text
/users
/removeuser 123456789
```

## Command Functions

- `/start`: opens the main tracker menu.
- `/status`: shows tracked products and their latest status.
- `/list`: shows tracked products with Amazon links.
- `/add`: asks for an Amazon product URL.
- `/rename 2 Name`: renames the action buttons for product 2.
- `/check`: opens the product check picker.
- `/delete`: opens the product delete picker.
- `/cancel`: clears stuck check state and returns to main menu.
- `/pause`: pauses scheduled checks for that user.
- `/resume`: resumes scheduled checks for that user.
- `/help`: shows command help.
- `/users`: admin list of approved, pending, and rejected users.
- `/removeuser`: admin removes user access but keeps their data as backup.

## Rename Behavior

`/rename` does not rename the real Amazon product title.

Example:

```text
/rename 2 vvv
```

This changes:

- `🔍 Check Product 2` to `🔍 Check vvv`
- `🗑 Delete Product 2` to `🗑 Delete vvv`

Status and product list still show the real Amazon product name.

Button names are stored in user settings, separate from product data, so scheduled stock checks cannot overwrite them.

## Delete Behavior

Deleting is handled by inline buttons from the Telegram menu.

Flow:

1. Tap `🗑 Delete Amazon Product`.
2. Bot shows `🗑 Delete Product 1`, `🗑 Delete Product 2`, etc.
3. Tap the product to delete.
4. Bot removes it and confirms the deletion.

If a product is deleted, saved button names are reindexed so labels stay attached to the product that moved up.

## Availability Detection

The scraper uses lightweight HTTP requests instead of Selenium/Chrome.

Why Selenium was removed:

- Selenium and Chromium used too much memory on Render free tier.
- Render free instances have a 512 MB memory limit.
- Selenium caused repeated out-of-memory crashes.

Current scraper checks Amazon page HTML for strong signals:

- available if Buy/Add to Cart signals are present
- unavailable if currently unavailable/no featured offer style signals are present
- unclear if Amazon does not show a reliable answer

## Price Tracking

Price is optional and safe.

Rules:

- Price is stored only when the product is clearly available.
- If product is unavailable or unclear, price is set to unknown/not found.
- Price scraping failures do not break availability checking.

## Notifications

Users can choose:

- Alert me every check
- Alert only when stock changes

Manual checks send:

1. check started
2. final check result

Manual checks do not send an extra duplicate availability alert.

Scheduled checks send alerts based on the user's notification mode.

## Scheduler

The scheduler wakes every 1 minute and checks which users are due based on their interval.

Each user can choose:

- 15 minutes
- 30 minutes
- 60 minutes

The scheduler:

- skips paused users
- shares URL checks across users
- scrapes the same URL once if multiple users track it
- prevents overlapping checks with locks

## Threading Fixes

The project includes locks for:

- check execution
- schedule operations

This prevents manual checks and scheduled checks from corrupting product state at the same time.

## Health Endpoint

Public endpoint:

```text
/
```

Shows safe public data only:

- service status
- scraper status
- approved user count
- pending user count
- total product count
- fresh/stale product count
- limits

It does not expose:

- product names
- product URLs
- user names
- chat IDs

## Dashboard

Public dashboard:

```text
/dashboard
```

Shows the same safe health information in a browser-friendly layout.

Dashboard scraper states:

- Healthy: all products fresh
- Partial: some products fresh, some stale/unclear
- Needs Check: most/all products stale

## Important Environment Variables

```text
TELEGRAM_BOT_TOKEN
ADMIN_CHAT_ID
REDIS_URL
PORT
```

Optional old seed variables may exist, but products are now managed from Telegram:

```text
PRODUCT_URL
ADDITIONAL_PRODUCT_URLS
```

## Current File Roles

- `app.py`: Flask app, Telegram control loop, scheduler, multi-user logic.
- `tracker.py`: Amazon scraper and product availability/price extraction.
- `notifier.py`: Telegram API sending, buttons, bot commands.
- `storage.py`: Redis/local fallback storage.
- `config.py`: environment variable config and limits.
- `requirements.txt`: Python dependencies.
- `Dockerfile`: Render deployment container.
- `README.md`: setup and deployment notes.
- `PROJECT_SUMMARY.md`: full project summary.

## Known Limitations

- Amazon can still block or change HTML at any time.
- Free Render may sleep if UptimeRobot is not active.
- Free Render memory is limited.
- Telegram availability may depend on local network restrictions.
- Price scraping is best-effort only.

## Current Deployment Flow

1. Code is changed locally.
2. Changes are committed to Git.
3. Commit is pushed to GitHub main branch.
4. Render auto-deploys the new version.
5. UptimeRobot pings the Render URL to keep it awake.
