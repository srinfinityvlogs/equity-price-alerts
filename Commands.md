# Equity Notifications — Bot Commands

Send these commands directly in your Telegram notification group. The bot only responds to commands sent in the group it's configured for — commands from anywhere else are ignored.

---

## `/addstock` — Add a stock to your watchlist

```
/addstock <SYMBOL> <TARGET_PRICE> <above|below>
```

**Example:**
```
/addstock RELIANCE.NS 1400 above
```
This alerts you once when Reliance's price rises above ₹1400.

**Symbol format:** use the Yahoo Finance ticker —
- NSE stocks need a `.NS` suffix: `TCS.NS`, `RELIANCE.NS`, `INFY.NS`
- BSE stocks need a `.BO` suffix: `RELIANCE.BO`

**Two targets on the same stock:** add it twice with different conditions — this tracks both independently:
```
/addstock RELIANCE.NS 1400 above
/addstock RELIANCE.NS 1250 below
```
You'll get separate alerts if either target is hit — one when it rises above ₹1400, another (independently) if it ever falls below ₹1250.

**Adding the same symbol + same condition again** replaces the old target with the new one (e.g. running `/addstock RELIANCE.NS 1450 above` after the example above updates your "above" target to ₹1450, rather than adding a third entry).

The bot checks the symbol is valid (has real price data) before adding it — if you get the ticker wrong, it'll tell you immediately instead of silently failing later.

---

## `/removestock` — Remove a stock (or one of its targets)

```
/removestock <SYMBOL>              # removes ALL targets for this stock
/removestock <SYMBOL> <above|below>  # removes only that specific target
```

**Examples:**
```
/removestock RELIANCE.NS            # removes both the above AND below targets
/removestock RELIANCE.NS above      # removes only the "above ₹1400" target, leaves "below ₹1250" active
```

---

## `/liststocks` — See your current watchlist

```
/liststocks
```

Replies with every stock currently being tracked, its target, and condition. Example output:
```
📋 Current watchlist:
- TCS.NS: above ₹2500.0
- RELIANCE.NS: above ₹1400.0
- RELIANCE.NS: below ₹1250.0
```

---

## `/help` — Quick reminder of available commands

```
/help
```

---

## Good to know

- **Adding/removing stocks works anytime** — day or night, market open or closed.
- **Price checks only run during NSE market hours**: 9:00 AM – 3:30 PM IST, Monday–Friday. Outside these hours, your watchlist just sits idle until the next trading session — no false alerts, no wasted checks.
- **One alert per target, per day.** Once a target is hit and you're alerted, it won't alert again for that same target until the next trading day — even if the price keeps moving past it.
- Prices come from Yahoo Finance (free, unofficial NSE/BSE data feed) — there can be a short delay (usually under a minute) compared to real-time exchange prices.