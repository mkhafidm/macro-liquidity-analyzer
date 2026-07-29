"""
Builds a full macro liquidity snapshot by combining market prices, macro
economic data, and news, then persists it for downstream consumption
(LLM analysis, Telegram reports, etc).

`main()` is the primary production entry point (called by run_scheduled.py).
`refresh_market_only()` is a lighter-weight variant that updates just the
market_prices section without touching macro/news or history.
"""

import json
import os
import logging
from datetime import datetime, timedelta

from fetcher.market_price_fetcher import fetch_market_price, format_market_report
from fetcher.news_fetcher import fetch_news_sentiment, fetch_news_geopolitic
from fetcher.macro_fetcher import fetch_macro_data
from config import SNAPSHOT_FILE, HISTORY_FILE, HISTORY_RETENTION_DAYS

logger = logging.getLogger(__name__)


def save_snapshot(snapshot: dict) -> None:
    os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)


def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        logger.warning("Failed to parse %s, starting with empty history", HISTORY_FILE)
        return []


def append_history(snapshot: dict) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)

    history = _load_history()
    history.append(snapshot)

    # Keep only entries within the retention window.
    cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    history = [h for h in history if h.get("timestamp", "") >= cutoff]

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, default=str)


def refresh_market_only() -> None:
    """
    Partial refresh: update only market_prices in the latest snapshot.
    Does not touch macro/news data and does not append to history.
    """
    try:
        raw_prices = fetch_market_price()
        market_report = format_market_report(raw_prices)
    except Exception as e:
        logger.error("Market refresh failed: %s", e)
        raise  # let the caller's error handler deal with it

    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE, "r") as f:
                snapshot = json.load(f)
        except Exception:
            logger.warning("Existing snapshot file corrupt, starting fresh")
            snapshot = {}
    else:
        snapshot = {}

    snapshot["market_prices"] = market_report
    snapshot["timestamp"] = datetime.now().isoformat()

    save_snapshot(snapshot)
    logger.info("Market prices refreshed (snapshot updated).")


def main() -> None:
    """Full refresh: market + macro + news, saved as latest snapshot and appended to history."""
    logger.info("Building full macro liquidity snapshot...")

    try:
        raw_prices = fetch_market_price()
        market_report = format_market_report(raw_prices)
    except Exception as e:
        logger.error("Market prices fetch failed: %s", e)
        market_report = {}

    try:
        macro_data = fetch_macro_data()
    except Exception as e:
        logger.error("Macro data fetch failed: %s", e)
        macro_data = {"actuals": {}, "calendar": {}, "alerts": []}

    try:
        sentiment_news = fetch_news_sentiment()
    except Exception as e:
        logger.error("Sentiment news fetch failed: %s", e)
        sentiment_news = []

    try:
        geo_news = fetch_news_geopolitic()
    except Exception as e:
        logger.error("Geopolitical news fetch failed: %s", e)
        geo_news = []

    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "market_prices": market_report,
        "macro_actuals": macro_data.get("actuals", {}),
        "macro_calendar": macro_data.get("calendar", {}),
        "macro_alerts": macro_data.get("alerts", []),
        "sentiment_news": sentiment_news,
        "geopolitical_news": geo_news,
    }

    save_snapshot(snapshot)
    append_history(snapshot)

    logger.info("Snapshot saved at %s", datetime.now().strftime("%H:%M:%S"))
    logger.info("sentiment_news: %d, geo_news: %d", len(sentiment_news), len(geo_news))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()