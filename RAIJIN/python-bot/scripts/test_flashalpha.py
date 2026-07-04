import os
from dotenv import load_dotenv
import requests

load_dotenv()

api_key = os.getenv("FLASHALPHA_API_KEY")
if not api_key:
    print("❌ FLASHALPHA_API_KEY not found in .env")
    exit(1)

BASE = "https://lab.flashalpha.com"
headers = {"X-Api-Key": api_key}

# Free tier: GEX and levels work for individual stocks
# ETFs (SPY, QQQ) require Basic+ plan
test_symbols_free = ["AAPL", "NVDA", "TSLA"]
test_symbols_etf = ["SPY", "QQQ"]

# Test 1: GEX for individual stocks (free tier)
print("=" * 50)
print("TEST 1: GEX — Individual Stocks (Free Tier)")
print("=" * 50)
for sym in test_symbols_free:
    try:
        r = requests.get(f"{BASE}/v1/exposure/gex/{sym}", headers=headers)
        if r.status_code == 200:
            data = r.json()
            net_gex = data.get("net_gex", "N/A")
            flip = data.get("gamma_flip", "N/A")
            label = data.get("net_gex_label", "N/A")
            print(f"  ✅ {sym}: Net GEX=${net_gex:,.0f} | Gamma Flip={flip} | Regime={label}")
        else:
            print(f"  ❌ {sym}: HTTP {r.status_code} — {r.text[:100]}")
    except Exception as e:
        print(f"  ❌ {sym}: {e}")

# Test 2: GEX for ETFs (requires Basic+)
print("\n" + "=" * 50)
print("TEST 2: GEX — ETFs (Basic+ Required)")
print("=" * 50)
for sym in test_symbols_etf:
    try:
        r = requests.get(f"{BASE}/v1/exposure/gex/{sym}", headers=headers)
        if r.status_code == 200:
            data = r.json()
            net_gex = data.get("net_gex", "N/A")
            flip = data.get("gamma_flip", "N/A")
            label = data.get("net_gex_label", "N/A")
            print(f"  ✅ {sym}: Net GEX=${net_gex:,.0f} | Gamma Flip={flip} | Regime={label}")
        else:
            print(f"  ⚠️  {sym}: HTTP {r.status_code} (expected if free tier)")
    except Exception as e:
        print(f"  ❌ {sym}: {e}")

# Test 3: Key levels
print("\n" + "=" * 50)
print("TEST 3: Key Levels")
print("=" * 50)
for sym in test_symbols_free:
    try:
        r = requests.get(f"{BASE}/v1/exposure/levels/{sym}", headers=headers)
        if r.status_code == 200:
            data = r.json()
            print(f"  ✅ {sym}: Gamma Flip={data.get('gamma_flip')} | Call Wall={data.get('call_wall')} | Put Wall={data.get('put_wall')}")
        else:
            print(f"  ❌ {sym}: HTTP {r.status_code} — {r.text[:100]}")
    except Exception as e:
        print(f"  ❌ {sym}: {e}")

# Test 4: Account info
print("\n" + "=" * 50)
print("TEST 4: Account Info")
print("=" * 50)
try:
    r = requests.get(f"{BASE}/v1/account", headers=headers)
    if r.status_code == 200:
        data = r.json()
        print(f"  ✅ Plan: {data.get('plan', 'N/A')} | Requests today: {data.get('requests_today', 'N/A')} | Limit: {data.get('daily_limit', 'N/A')}")
    else:
        print(f"  ❌ HTTP {r.status_code} — {r.text[:100]}")
except Exception as e:
    print(f"  ❌ {e}")

print("\n" + "=" * 50)
print("ALL FLASHALPHA TESTS COMPLETE")
print("=" * 50)
