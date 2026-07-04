import os
from dotenv import load_dotenv
from polygon import RESTClient

load_dotenv()

api_key = os.getenv("POLYGON_API_KEY")
if not api_key:
    print("❌ POLYGON_API_KEY not found in .env")
    exit(1)

client = RESTClient(api_key=api_key)
watchlist = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]

# Test 1: Ticker details
print("=" * 50)
print("TEST 1: Ticker Details")
print("=" * 50)
for ticker in watchlist:
    try:
        details = client.get_ticker_details(ticker)
        print(f"  ✅ {ticker}: {details.name} | Market: {details.market}")
    except Exception as e:
        print(f"  ❌ {ticker}: {e}")

# Test 2: Options chain snapshot
print("\n" + "=" * 50)
print("TEST 2: Options Chain Snapshot (SPY)")
print("=" * 50)
try:
    contracts = []
    for c in client.list_snapshot_options_chain("SPY"):
        contracts.append(c)
        if len(contracts) >= 5:
            break
    for c in contracts:
        d = c.details
        g = c.greeks
        print(f"  ✅ {d.ticker} | Strike: {d.strike_price} | Exp: {d.expiration_date}")
        if g:
            print(f"     Greeks — Δ:{g.delta:.4f} Γ:{g.gamma:.6f} Θ:{g.theta:.4f} ν:{g.vega:.4f} IV:{c.implied_volatility:.4f}")
        else:
            print(f"     Greeks — not available")
    print(f"  Loaded {len(contracts)} contracts (capped at 5 for test)")
except Exception as e:
    print(f"  ❌ Options chain failed: {e}")

# Test 3: Historical IV via aggregates
print("\n" + "=" * 50)
print("TEST 3: SPY Price Aggregates (last 5 days)")
print("=" * 50)
try:
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    aggs = list(client.list_aggs("SPY", 1, "day", start, end))
    for a in aggs:
        dt = datetime.fromtimestamp(a.timestamp / 1000).strftime("%Y-%m-%d")
        print(f"  ✅ {dt} | O:{a.open} H:{a.high} L:{a.low} C:{a.close} V:{a.volume}")
except Exception as e:
    print(f"  ❌ Aggregates failed: {e}")

print("\n" + "=" * 50)
print("ALL POLYGON TESTS COMPLETE")
print("=" * 50)
