"""
Configuration for the macro liquidity analyzer.

All values are loaded from environment variables (.env file).
Sensitive values (API keys, tokens) should NEVER be hardcoded here.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _parse_keys(env_var: str) -> list[str]:
    """Parse comma-separated API keys from environment variable."""
    raw = os.getenv(env_var, "")
    return [k.strip() for k in raw.split(",") if k.strip()]


# ============================================================
# TELEGRAM
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID")      
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")     


# ============================================================
# API KEYS (multi-key support for rotation)
# ============================================================
QUANTGIST_API_KEYS = _parse_keys("QUANTGIST_API_KEYS")
GROQ_API_KEYS = _parse_keys("GROQ_API_KEYS")


# ============================================================
# QUANTGIST API
# ============================================================
QUANTGIST_BASE_URL = "https://api.quantgist.com"


# ============================================================
# MACRO FETCHER
# ============================================================
LAST_SEEN_FILE = "data/last_seen_values.json"

# Which macro events to track
DEFAULT_ACTUAL_EVENTS = ["NFP", "CPI", "FOMC", "GDP", "PCE"]
DEFAULT_CALENDAR_EVENTS = ["NFP", "CPI", "FOMC", "GDP", "PCE", "ISM", "RETAIL_SALES"]


# ============================================================
# MARKET DATA (yfinance)
# ============================================================
MARKET_ASSETS = {
    "DXY": "DX-Y.NYB",
    "US10Y": "^TNX",
    "Gold": "GC=F",
    "BTC": "BTC-USD",
    "SP500": "^GSPC",
    "Nasdaq": "^IXIC",
    "Crude_Oil": "CL=F",
    "VIX": "^VIX",
}

# These are derived from MARKET_ASSETS and rarely change — keeping them
# here is fine, but they're also safe to move into market_price_fetcher.py.
NYSE_TIED_ASSETS = {"SP500", "Nasdaq", "VIX"}
FUTURES_FX_ASSETS = {"DXY", "US10Y", "Gold", "Crude_Oil"}
DOLLAR_VOLUME_ASSETS = {"BTC", "Gold", "Crude_Oil"}

MARKET_HISTORY_PERIOD = "2y"
MARKET_HISTORY_INTERVAL = "1d"


# ============================================================
# NEWS FETCHER
# ============================================================
NEWS_CHECKPOINT_FILE = "data/news_checkpoint.json"
NEWS_SENTIMENT_QUERY = "Fed"


# ============================================================
# ORCHESTRATOR
# ============================================================
SNAPSHOT_FILE = "data/latest_snapshot.json"
HISTORY_FILE = "data/history_snapshots.json"
HISTORY_RETENTION_DAYS = 14


# ============================================================
# PIPELINE
# ============================================================
NEWS_ARCHIVE_FILE = "data/news_archive.json"
ANALYSIS_RESULT_FILE = "data/analysis_result.txt"
NEWS_ARCHIVE_RETENTION_DAYS = 7
NEWS_TOP_N_SENTIMENT_HOURS = 48
NEWS_TOP_N_GEO_HOURS = 72
NEWS_TOP_N_LIMIT_EACH = 15


# ============================================================
# LLM / GROQ
# ============================================================
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TEMPERATURE = 0.3
GROQ_MAX_TOKENS = 2048
GROQ_SYSTEM_PROMPT = "Anda adalah analis finansial senior. Jawab dalam bahasa Indonesia profesional. Jangan mengarang data."