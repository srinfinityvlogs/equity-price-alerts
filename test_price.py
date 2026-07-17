import yfinance as yf

symbols = ["RELIANCE.NS", "TCS.NS"]

for symbol in symbols:
    ticker = yf.Ticker(symbol)
    data = ticker.history(period="1d", interval="1m")
    if not data.empty:
        latest_price = data["Close"].iloc[-1]
        latest_time = data.index[-1]
        print(f"{symbol}: ₹{latest_price:.2f} (as of {latest_time})")
    else:
        print(f"{symbol}: No data returned — market may be closed, or symbol is wrong")
