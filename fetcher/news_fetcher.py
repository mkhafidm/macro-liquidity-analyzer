"""
Fetches news from the QuantGist API for two purposes:
- Sentiment news (/v1/news): general Fed/monetary-policy headlines.
- Geopolitical risk clusters (/v1/news/radar): grouped events per topic.
  Note: /v1/news/radar is a paid endpoint; requests may return None if the
  account's quota for it is exhausted (handled gracefully by api_client).

Both fetchers use a time-based checkpoint (data/news_checkpoint.json) to
only return items newer than the last successful run.
"""

import os
import json
import hashlib
import time
import random
import logging
from datetime import datetime

from utils.api_client import safe_quantgist_request
from config import (
    QUANTGIST_BASE_URL,
    NEWS_CHECKPOINT_FILE,
    NEWS_SENTIMENT_QUERY,
)

logger = logging.getLogger(__name__)
GEOPOLITICAL_TOPICS = [
    "iran-war",
    "oil-supply",
    "sanctions",
    "middle-east-risk",
    "trump-posts",
    "central-bank-unscheduled",
    "ceasefire",
]
GEOPOLITICAL_MIN_IMPACT = 0.4
GEOPOLITICAL_LOOKBACK_HOURS = 72
GEOPOLITICAL_LIMIT = 50
NEWS_IMPACT_SCORE_THRESHOLD = 0.25


def load_checkpoints() -> dict:
    default = {"sentiment_last_time": None, "geopolitic_last_time": None}
    if os.path.exists(NEWS_CHECKPOINT_FILE):
        try:
            with open(NEWS_CHECKPOINT_FILE, "r") as f:
                data = json.load(f)
                return {
                    "sentiment_last_time": data.get("sentiment_last_time"),
                    "geopolitic_last_time": data.get("geopolitic_last_time"),
                }
        except Exception:
            logger.warning("Failed to parse %s, using default checkpoints", NEWS_CHECKPOINT_FILE)
    return default


def save_checkpoints(sentiment_last_time, geopolitic_last_time) -> None:
    data = {
        "sentiment_last_time": sentiment_last_time,
        "geopolitic_last_time": geopolitic_last_time,
    }
    os.makedirs(os.path.dirname(NEWS_CHECKPOINT_FILE), exist_ok=True)
    with open(NEWS_CHECKPOINT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _parse_iso(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_news_sentiment(query: str = NEWS_SENTIMENT_QUERY) -> list:
    """Fetch new sentiment/Fed-related headlines since the last checkpoint."""
    checkpoints = load_checkpoints()
    last_time_str = checkpoints["sentiment_last_time"]
    last_time = _parse_iso(last_time_str)

    url = f"{QUANTGIST_BASE_URL}/v1/news"
    all_new_items = []
    max_time = last_time

    for page in [1, 2]:
        params = {"q": query, "limit": 30, "page": page}
        try:
            data = safe_quantgist_request(url, params)
            if data is None:
                continue

            raw_news = data.get("data", [])
            if not raw_news:
                logger.info("Page %d: no items returned", page)
                continue

            logger.info("Page %d: %d raw items", page, len(raw_news))

            for item in raw_news:
                pub_time = _parse_iso(item.get("published_at"))
                if pub_time is None:
                    continue
                if last_time and pub_time <= last_time:
                    continue

                all_new_items.append(item)
                if max_time is None or pub_time > max_time:
                    max_time = pub_time

            time.sleep(random.uniform(0.2, 1.0))

        except Exception as e:
            logger.error("Page %d error: %s", page, e)

    if not all_new_items:
        logger.info("No new sentiment news (last_time=%s)", last_time_str)
        return []

    logger.info("Total %d new items fetched", len(all_new_items))

    # Filter to high/medium impact with a meaningful score. sentiment_score
    # and sentiment_label from QuantGist are intentionally not used here:
    # impact_score already blends sentiment, magnitude, and source
    # credibility. impact (high/medium/low) is used for fast filtering, and
    # symbols is kept for matching against market assets downstream.
    filtered_news = [
        item for item in all_new_items
        if item.get("impact") in ("high", "medium")
        and item.get("impact_score") is not None
        and item.get("impact_score", 0) > NEWS_IMPACT_SCORE_THRESHOLD
    ]

    output = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "summary": item.get("summary"),
            "published_at": item.get("published_at"),
            "impact": item.get("impact"),
            "impact_score": item.get("impact_score"),
            "topic_matches": item.get("topic_matches", []),
            "symbols": item.get("symbols", []),
            "source": item.get("source"),
            "url": item.get("source_url"),
        }
        for item in filtered_news
    ]

    if output:
        max_time_str = max_time.isoformat() if max_time else None
        save_checkpoints(max_time_str, checkpoints.get("geopolitic_last_time"))
        logger.info("Sentiment: %d new items above threshold (last_time=%s)", len(output), max_time_str)
    else:
        logger.info("No items met the high/medium impact + score threshold")

    return output


def fetch_news_geopolitic(
    topics: list = None,
    min_impact: float = GEOPOLITICAL_MIN_IMPACT,
    lookback_hours: int = GEOPOLITICAL_LOOKBACK_HOURS,
    limit: int = GEOPOLITICAL_LIMIT,
) -> list:
    """Fetch new geopolitical risk clusters since the last checkpoint."""
    topics = topics or GEOPOLITICAL_TOPICS

    checkpoints = load_checkpoints()
    last_time_str = checkpoints["geopolitic_last_time"]
    last_time = _parse_iso(last_time_str)

    url = f"{QUANTGIST_BASE_URL}/v1/news/radar"
    all_items = []

    logger.info("Fetching %d geopolitical topics...", len(topics))

    for topic in topics:
        params = {
            "topic": topic,
            "min_impact": min_impact,
            "lookback_hours": lookback_hours,
            "limit": limit,
            "event_type": "geopolitical_risk",
        }
        try:
            data = safe_quantgist_request(url, params, timeout=15)
            if data is None:
                continue

            items = data.get("items", [])
            if items:
                logger.info("  %s: %d cluster(s)", topic, len(items))
                all_items.extend(items)
            else:
                logger.info("  %s: no clusters", topic)

            time.sleep(random.uniform(0.5, 1.5))

        except Exception as e:
            logger.error("  %s error: %s", topic, e)

    if not all_items:
        logger.info("No geopolitical clusters returned")
        return []

    logger.info("Total %d raw clusters", len(all_items))

    seen_ids = set()
    new_items = []
    max_time = last_time

    for item in all_items:
        latest_seen = _parse_iso(item.get("latest_seen"))
        if latest_seen is None:
            continue
        if last_time and latest_seen <= last_time:
            continue

        # Dedupe across topics: the same cluster can be returned by more than
        # one topic query, so hash on headline + first_seen instead of relying
        # on a per-topic ID.
        raw_id = f"{item.get('headline', '')}_{item.get('first_seen', '')}"
        news_id = hashlib.md5(raw_id.encode()).hexdigest()
        if news_id in seen_ids:
            continue
        seen_ids.add(news_id)

        new_items.append({
            "id": news_id,
            "headline": item.get("headline", ""),
            "why_it_matters": item.get("why_it_matters", ""),
            "published_at": item.get("latest_seen"),
            "impact_score": item.get("impact_score", 0),
            "affected_assets": item.get("affected_assets", []),
            "topic": item.get("topic"),
            "status": item.get("status"),
            "confidence": item.get("confidence"),
            "source_count": item.get("source_count", 0),
        })

        if max_time is None or latest_seen > max_time:
            max_time = latest_seen

    if not new_items:
        logger.info("No new geopolitical clusters (last_time=%s)", last_time_str)
        return []

    new_items.sort(key=lambda x: x.get("impact_score", 0), reverse=True)

    max_time_str = max_time.isoformat() if max_time else None
    save_checkpoints(checkpoints.get("sentiment_last_time"), max_time_str)
    logger.info("Geopolitical: %d new cluster(s) (last_time=%s)", len(new_items), max_time_str)

    return new_items


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    print("=" * 50)
    print("TEST NEWS FETCHER")
    print("=" * 50)

    print("\n[1] Sentiment (Fed):")
    sent = fetch_news_sentiment()
    print(f"  -> {len(sent)} new item(s)")
    if sent:
        print(f"  Sample: {sent[0]['title'][:50]}... | score: {sent[0]['impact_score']}")

    print("\n[2] Geopolitical:")
    geo = fetch_news_geopolitic()
    print(f"  -> {len(geo)} new cluster(s)")
    if geo:
        print(f"  Sample: {geo[0]['headline'][:50]}... | score: {geo[0]['impact_score']}")

    print("\n" + "=" * 50)
    print(f"TOTAL: {len(sent) + len(geo)} new item(s)")
    print("=" * 50)