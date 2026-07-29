"""
Loads the latest snapshot, maintains a rolling news archive (deduped across
fetch cycles), selects the top-N most relevant news items, and generates
the final AI analysis via llm_analyst.

run_analysis() is the primary entry point (called by run_scheduled.py).
"""

import json
import os
import logging
from datetime import datetime, timedelta

from brain.llm_analyst import generate_analysis
from config import (
    SNAPSHOT_FILE,
    NEWS_ARCHIVE_FILE,
    ANALYSIS_RESULT_FILE,
    NEWS_ARCHIVE_RETENTION_DAYS,
    NEWS_TOP_N_SENTIMENT_HOURS,
    NEWS_TOP_N_GEO_HOURS,
    NEWS_TOP_N_LIMIT_EACH,
)

logger = logging.getLogger(__name__)


def load_snapshot() -> dict:
    if not os.path.exists(SNAPSHOT_FILE):
        raise FileNotFoundError(f"Snapshot not found: {SNAPSHOT_FILE}")
    with open(SNAPSHOT_FILE, "r") as f:
        return json.load(f)


def _load_archive() -> dict:
    if not os.path.exists(NEWS_ARCHIVE_FILE):
        return {"sentiment": [], "geopolitic": []}
    try:
        with open(NEWS_ARCHIVE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        logger.warning("Failed to parse %s, starting with empty archive", NEWS_ARCHIVE_FILE)
        return {"sentiment": [], "geopolitic": []}


def append_news_archive(sentiment_news: list, geo_news: list, keep_days: int = NEWS_ARCHIVE_RETENTION_DAYS) -> dict:
    """Accumulate news across fetch cycles, deduped by id, pruned by age."""
    os.makedirs(os.path.dirname(NEWS_ARCHIVE_FILE), exist_ok=True)
    archive = _load_archive()

    def merge_dedup(existing, new_items):
        seen = {item.get("id") for item in existing if item.get("id")}
        for item in new_items:
            key = item.get("id")
            if key and key not in seen:
                existing.append(item)
                seen.add(key)
        return existing

    archive["sentiment"] = merge_dedup(archive.get("sentiment", []), sentiment_news)
    archive["geopolitic"] = merge_dedup(archive.get("geopolitic", []), geo_news)

    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    archive["sentiment"] = [n for n in archive["sentiment"] if n.get("published_at", "") >= cutoff]
    archive["geopolitic"] = [n for n in archive["geopolitic"] if n.get("published_at", "") >= cutoff]

    with open(NEWS_ARCHIVE_FILE, "w") as f:
        json.dump(archive, f, indent=2, default=str)

    return archive


def get_top_news(
    hours_sentiment: int = NEWS_TOP_N_SENTIMENT_HOURS,
    hours_geo: int = NEWS_TOP_N_GEO_HOURS,
    limit_each: int = NEWS_TOP_N_LIMIT_EACH,
) -> tuple:
    """Return the top-N most impactful news items within a recency window, per category."""
    if not os.path.exists(NEWS_ARCHIVE_FILE):
        return [], []

    with open(NEWS_ARCHIVE_FILE, "r") as f:
        archive = json.load(f)

    def filter_and_sort(news_list, hours):
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        filtered = [n for n in news_list if n.get("published_at", "") > cutoff]
        return sorted(filtered, key=lambda x: x.get("impact_score", 0), reverse=True)

    sentiment_top = filter_and_sort(archive.get("sentiment", []), hours_sentiment)[:limit_each]
    geo_top = filter_and_sort(archive.get("geopolitic", []), hours_geo)[:limit_each]

    return sentiment_top, geo_top


def run_analysis(refresh_first: bool = False) -> str:
    """Main pipeline: load snapshot -> archive news -> select top-N -> generate AI analysis."""
    logger.info("Starting analysis...")

    if refresh_first:
        from fetcher.orchestrator import main as refresh_snapshot
        logger.info("Refreshing snapshot via orchestrator...")
        refresh_snapshot()

    try:
        snapshot = load_snapshot()
    except FileNotFoundError as e:
        logger.error(str(e))
        return f"❌ {e}. Run the orchestrator first."

    append_news_archive(
        snapshot.get("sentiment_news", []),
        snapshot.get("geopolitical_news", []),
    )
    sentiment_top, geo_top = get_top_news()
    logger.info("Sentiment news: %d, Geo news: %d", len(sentiment_top), len(geo_top))

    prompt_data = {
        "timestamp": snapshot.get("timestamp"),
        "market_prices": snapshot.get("market_prices", {}),
        "macro_actuals": snapshot.get("macro_actuals", {}),
        "macro_calendar": snapshot.get("macro_calendar", {}),
        "macro_alerts": snapshot.get("macro_alerts", []),
        "sentiment_news": sentiment_top,
        "geopolitical_news": geo_top,
    }

    result = generate_analysis(prompt_data)

    if result.startswith("❌"):
        logger.error("Analysis failed: %s", result)
        return result

    os.makedirs(os.path.dirname(ANALYSIS_RESULT_FILE), exist_ok=True)
    with open(ANALYSIS_RESULT_FILE, "w", encoding="utf-8") as f:
        f.write(result)
    logger.info("Analysis saved to %s", ANALYSIS_RESULT_FILE)

    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    output = run_analysis()
    print("\n" + "=" * 70)
    print("AI ANALYSIS:")
    print("=" * 70)
    print(output)
    print("=" * 70)