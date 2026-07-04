import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Environment
    ENV = os.getenv("ENV", "paper")

    # IBKR
    IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
    IBKR_PAPER_PORT = int(os.getenv("IBKR_PAPER_PORT", 7497))
    IBKR_LIVE_PORT = int(os.getenv("IBKR_LIVE_PORT", 7496))

    # Cloudflare Worker → Bot auth
    INTERNAL_SECRET = os.getenv("INTERNAL_SECRET", "")

    # Polygon / Massive
    POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
    POLYGON_BASE_URL = os.getenv("POLYGON_BASE_URL", "https://api.polygon.io")

    # FlashAlpha (optional)
    FLASHALPHA_API_KEY = os.getenv("FLASHALPHA_API_KEY", "")
    FLASHALPHA_BASE_URL = os.getenv("FLASHALPHA_BASE_URL", "https://lab.flashalpha.com")

    # Risk params (locked)
    MAX_RISK_PER_TRADE = 0.05
    MAX_RISK_DOLLARS = 250
    MAX_POSITIONS = 3
    MAX_DAILY_DD = 0.02
    MAX_PEAK_TROUGH = 0.08
    MAX_SPREAD_WIDTH = 3
    PDT_LIMIT = 3
    KELLY_FRACTION = 0.25
    PROFIT_TARGET = 0.50
    STOP_LOSS_MULT = 2.0
    VIX_SPIKE_THRESHOLD = 30

    # Watchlist
    WATCHLIST = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]

settings = Settings()
