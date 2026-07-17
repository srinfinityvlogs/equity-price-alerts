# Equity Price Alerts

A free, self-hosted Telegram bot that watches NSE/BSE stock prices and alerts you when they cross a target — plus lets you manage your watchlist entirely through Telegram chat commands, no code editing required after setup.

- Real-time-ish price checks (every 3 minutes) during NSE market hours (9:00 AM–3:30 PM IST, Mon–Fri)
- Add/remove stocks via `/addstock` and `/removestock` — anytime, market open or closed
- Two independent targets per stock supported (e.g. alert above ₹1400 AND below ₹1250)
- One alert per target per day — no repeat spam once triggered
- Free to run — Yahoo Finance data (no broker API key needed), deployable on any Docker-friendly free/cheap host

This is **your own independent instance** — clone it, plug in your own Telegram bot and chat, and it's entirely yours. Nothing is shared with the original deployment.

---

## How it works

A single Python script (`price_checker.py`) runs two things concurrently:
1. A price-checking loop — fetches prices via `yfinance`, compares against your `watchlist.json`, sends a Telegram alert when a target is crossed
2. A command-listener loop — polls Telegram for `/addstock`, `/removestock`, `/liststocks`, `/help` commands and updates `watchlist.json` live

See [COMMANDS.md](./COMMANDS.md) for the full command reference.

---

## Setup

### 1. Create your own Telegram Bot
1. Open Telegram, message **@BotFather**
2. Send `/newbot`, follow the prompts to name your bot
3. Save the **token** it gives you — you'll need it below

### 2. Create a Telegram group for your alerts
1. Create a new Telegram group (or reuse an existing private one)
2. Add your bot to the group
3. Send `/start` in the group (needed so the bot can "see" it)
4. Get the group's Chat ID:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id": ...}` in the response — that's your Chat ID (usually a negative number).

### 3. Clone this repo
```bash
git clone https://github.com/srinfinityvlogs/equity-price-alerts.git
cd equity-price-alerts
```

### 4. Set up your environment
```bash
cp .env.example .env
```
Edit `.env` and fill in your real values:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_group_chat_id_here
```

### 5. Set up your watchlist
```bash
cp watchlist.example.json watchlist.json
```
Edit `watchlist.json` with your real stocks, or just leave it as an empty list `[]` and add stocks later via `/addstock` in your Telegram group.

### 6. Run locally to test
```bash
python3 -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 price_checker.py
```
Try `/liststocks` in your Telegram group to confirm it's responding.

### 7. Deploy (so it runs 24/7 without your computer)
This repo includes a `Dockerfile`, so it can be deployed to any Docker-friendly host. A few free/cheap options:
- **Northflank** (Sandbox tier — free, always-on, no sleep) — connect this GitHub repo, build type: Dockerfile, no exposed ports needed, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as runtime environment variables
- **Fly.io** — no longer has a free tier, but cheap (~$2-5/month) for a small always-on machine
- **Your own VPS / home server / Raspberry Pi** — run via Docker or directly with the venv steps above

Whichever you choose, the two required environment variables are `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — no other configuration needed.

---

## Notes

- **Data source:** prices come from Yahoo Finance (`yfinance`), a free but unofficial feed — not directly tied to any broker account, so you maintain your watchlist independently of your actual portfolio.
- **NSE tickers** need a `.NS` suffix (e.g. `RELIANCE.NS`), **BSE tickers** need `.BO`.
- **Market hours are hardcoded to IST** (`Asia/Kolkata`) regardless of what timezone your server runs in.
- This bot only responds to commands sent in the specific group matching `TELEGRAM_CHAT_ID` — anyone else messaging your bot directly is ignored.
- Commands are currently **shared across everyone in the group** — there's no per-user watchlist separation. If multiple people use the same deployment, they share one watchlist.

## License

MIT — free to use, modify, and redeploy.# test
