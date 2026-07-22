# Equity Notifications — Bot Commands

Send these commands directly in your Telegram group. The bot only responds to commands sent in groups listed in `ALLOWED_CHAT_IDS` — each group has its own fully independent watchlist and reminders, stored in a shared Postgres database keyed by chat ID.

---

## Stock Watchlist

### `/addstock` — Add a stock target

```
/addstock <SYMBOL> <TARGET_PRICE> <above|below>
```

**Example:**
```
/addstock RELIANCE.NS 1400 above
```

**Symbol format:** Yahoo Finance ticker —
- NSE: `.NS` suffix (`TCS.NS`, `RELIANCE.NS`)
- BSE: `.BO` suffix (`RELIANCE.BO`)

**Multiple targets on the same stock and same condition are supported** — e.g. two separate "above" alerts at different prices:
```
/addstock KALYANKJIL.NS 580 above
/addstock KALYANKJIL.NS 600 above
```
Both are tracked independently. Adding the exact same symbol + condition + price again is a no-op (bot tells you it's already there).

The bot validates the symbol has real price data before adding it.

### `/removestock` — Remove a target

```
/removestock <SYMBOL>                      # removes ALL targets for this stock
/removestock <SYMBOL> <above|below>        # removes all targets with that condition
/removestock <SYMBOL> <above|below> <PRICE>  # removes just that one exact target
```

**Example:**
```
/removestock RELIANCE.NS below 1250
```

### `/liststocks` — View your watchlist

```
/liststocks
```

---

## Reminders (one-time, today only)

### `/remindme` — Set a reminder for later today

```
/remindme <TIME> [message]
```

**Time format:** no space before AM/PM — `2PM`, `2:30PM`, or 24-hour `14:00`, `09:15`.

**Examples:**
```
/remindme 2PM
/remindme 2:30PM Check the market before it closes
```

If no message is given, defaults to a generic market-check reminder. **This fires once, today, then deletes itself** — it does not repeat tomorrow. Setting a time that's already passed today is rejected with an error.

### `/removereminder` — Cancel a reminder set for today

```
/removereminder <TIME>
```

### `/listreminders` — See today's pending reminders

```
/listreminders
```

---

## `/help` — Quick command reference

```
/help
```

---

## Good to know

- **Adding/removing stocks and reminders works anytime** — day or night, market open or closed.
- **Price checks only run during NSE market hours**: 9:00 AM – 3:30 PM IST, Monday–Friday.
- **One price alert per target, per day** — won't repeat once triggered until the next trading day.
- **Reminders are one-time only** — set a new one each day you need it.
- **Data persists across redeploys** — watchlists and reminders are stored in Postgres, not lost when the bot is updated or restarted.
- **Each group is fully independent** — adding more friends' groups just means adding their chat ID to the bot's configuration; nothing is shared between groups.
- Prices come from Yahoo Finance (free, unofficial NSE/BSE data feed) — can lag real-time by up to a minute.
