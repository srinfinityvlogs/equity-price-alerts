# Equity Price Alerts

A free, self-hosted Telegram bot that watches NSE/BSE stock prices and alerts you when they cross a target — plus one-time reminders and full watchlist management, all through Telegram chat commands, no code editing required after setup.

- Real-time-ish price checks (every 3 minutes) during NSE market hours (9:00 AM–3:30 PM IST, Mon–Fri)
- Add/remove stocks via `/addstock` and `/removestock` — anytime, market open or closed
- Multiple independent targets per stock, including several at different prices with the same condition (e.g. two "above" alerts)
- One alert per target per day — no repeat spam once triggered
- One-time daily reminders via `/remindme` — set it for later today, fires once, then clears itself
- **Multiple independent groups on one deployment** — add friends' groups and each gets a fully separate watchlist and reminders, no data shared between them
- **Data persists across redeploys** — backed by Postgres, not lost when the bot is updated
- Free to run — Yahoo Finance data (no broker API key needed), deployable on any Docker-friendly host

This is **your own independent instance** — clone it, plug in your own Telegram bot and chat(s), and it's entirely yours.

---

## How it works

A single Python script (`price_checker.py`) runs three things concurrently:
1. **Price-checking loop** — fetches prices via `yfinance`, compares against each group's watchlist, sends a Telegram alert when a target is crossed
2. **Command-listener loop** — polls Telegram for `/addstock`, `/removestock`, `/liststocks`, `/remindme`, `/removereminder`, `/listreminders`, `/help` and updates the database live
3. **Reminder loop** — checks every 30 seconds for due one-time reminders and fires them

All state (watchlists, alert history, reminders, command polling offset) lives in Postgres, scoped per chat ID — so multiple Telegram groups can share one deployment while staying fully independent.

See [COMMANDS.md](./COMMANDS.md) for the full command reference.

---

## Setup

### 1. Create your own Telegram Bot
1. Message **@BotFather** on Telegram
2. Send `/newbot`, follow the prompts
3. Save the **token** it gives you

### 2. Create a Telegram group for each user/watchlist you want
For each group:
1. Create the group, add your bot to it
2. Send `/start` in the group
3. Get its Chat ID:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id": ...}` — usually a negative number.

Repeat for as many groups as you want (yourself, friends, etc.) — one bot can serve all of them.

### 3. Clone this repo
```bash
git clone https://github.com/srinfinityvlogs/equity-price-alerts.git
cd equity-price-alerts
```

### 4. Set up a Postgres database
Any Postgres instance works — a free addon on your hosting platform, a free tier from a managed provider, or a local instance for testing. You'll need: host, port, database name, username, password.

### 5. Set up your environment
```bash
cp .env.example .env
```
Fill in `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_CHAT_IDS=chat_id_1,chat_id_2,chat_id_3
PGHOST=your_db_host
PGPORT=5432
PGDATABASE=your_db_name
PGUSER=your_db_user
PGPASSWORD=your_db_password
```
`ALLOWED_CHAT_IDS` is comma-separated — add as many group chat IDs as you want; each gets an independent watchlist and reminders automatically.

### 6. Run locally to test
```bash
python3 -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 price_checker.py
```
You should see `[DB] Tables ready.` on startup — this confirms it connected and auto-created the required tables. Try `/liststocks` in a configured group to confirm it's responding.

**Note:** if your database is only reachable from within your hosting provider's internal network (common default for managed database addons), local testing won't connect — deploy first and test there instead.

### 7. Deploy (so it runs 24/7 without your computer)
This repo includes a `Dockerfile`, so it deploys to any Docker-friendly host. A few options:
- **Northflank** (Sandbox tier — free service compute, always-on, no sleep) — connect this GitHub repo, build type: Dockerfile, no exposed ports needed, add the environment variables above as runtime variables, and add a Postgres addon in the same project
- **Fly.io** — no longer has a free tier, but cheap (~$2-5/month) for a small always-on machine, plus their own Postgres offering
- **Your own VPS / home server / Raspberry Pi** — run via Docker or directly with the venv steps above, paired with any Postgres instance

---

## Adding more groups later

Since watchlists are keyed by chat ID in the database, adding a new group doesn't require any code changes:
1. Get the new group's chat ID (same process as Setup step 2)
2. Add it to `ALLOWED_CHAT_IDS` in your environment variables (comma-separated)
3. Redeploy/restart

The new group starts with an empty watchlist and no reminders — fully independent of every other group already using the bot.

---

## Symbol format (important)

- **NSE:** `TICKER.NS` — e.g. `RELIANCE.NS`, `TCS.NS`
- **BSE:** `TICKER.BO` — e.g. `AFCOM.BO`

For BSE stocks, always use the **trading ticker symbol**, not the numeric BSE scrip code shown on screener.in or BSE India (e.g. use `AFCOM`, not `544224`). The bot validates symbols via Yahoo Finance before adding them, so an invalid or unsupported ticker will be rejected immediately rather than silently failing later. Not every thinly-traded BSE stock is covered by Yahoo Finance — if a correctly formatted ticker still fails, Yahoo may simply not carry data for it.

---

## Notes

- **Data source:** prices come from Yahoo Finance (`yfinance`), a free but unofficial feed — not tied to any broker account.
- **Market hours are hardcoded to IST** (`Asia/Kolkata`) regardless of server timezone.
- The bot only responds to commands from chat IDs listed in `ALLOWED_CHAT_IDS` — everyone else is ignored.
- **Within a group, commands are shared** — there's no per-person separation inside a single group. If two people are in the same group, they share that group's one watchlist. For separate watchlists, use separate groups.

## License

MIT — free to use, modify, and redeploy.
