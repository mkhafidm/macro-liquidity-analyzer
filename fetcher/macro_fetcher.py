"""
Fetches macro economic data from the QuantGist API.

Design notes:
- /v1/macro/latest is used for actual released values.
- /v1/macro/calendar is used for upcoming release dates.
- The API's own `first_print`/`revision_seq` flags are unreliable (observed
  cases where `first_print: true` was attached to an already-revised value).
  Timing confidence and dedupe logic below intentionally avoid relying on them.
"""

import json
import time
import os
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from utils.api_client import safe_quantgist_request
from config import (
    QUANTGIST_BASE_URL,
    LAST_SEEN_FILE,
    DEFAULT_ACTUAL_EVENTS,
    DEFAULT_CALENDAR_EVENTS,
    TIMING_LIKELY_FIRST_PRINT_HOURS,
    TIMING_UNCERTAIN_MAX_HOURS,
)

logger = logging.getLogger(__name__)
TIMING_LIKELY_FIRST_PRINT_HOURS = 6
TIMING_UNCERTAIN_MAX_HOURS = 72


def _assess_timing_confidence(event_data: dict) -> str:
    """
    Estimate how likely `actual` reflects the true first-print value,
    based on the gap between release_time and published_at.

    The API's `first_print` flag can be True even on revised values, so this
    gap-based heuristic is used instead as the primary signal.

    Returns: "likely_first_print" | "uncertain" | "possibly_revised" | "unknown"
    """
    try:
        release_time = event_data.get("release_time")
        published_at = event_data.get("published_at")
        if not release_time or not published_at:
            return "unknown"

        rt = datetime.fromisoformat(release_time.replace("Z", "+00:00"))
        pa = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        gap_hours = (pa - rt).total_seconds() / 3600

        if gap_hours <= TIMING_LIKELY_FIRST_PRINT_HOURS:
            return "likely_first_print"
        elif gap_hours <= TIMING_UNCERTAIN_MAX_HOURS:
            return "uncertain"
        return "possibly_revised"
    except Exception:
        return "unknown"


def fetch_macro_actuals(aliases: Optional[List[str]] = None) -> Dict[str, dict]:
    """Fetch the latest released value for each alias from /v1/macro/latest."""
    aliases = aliases or DEFAULT_ACTUAL_EVENTS
    url = f"{QUANTGIST_BASE_URL}/v1/macro/latest"
    results = {}

    for alias in aliases:
        data = safe_quantgist_request(url, {"event": alias})

        if data is None:
            logger.warning("No response from API for %s", alias)
            results[alias] = {"status": "error", "detail": "No response from API"}
            time.sleep(1)
            continue

        event_data = data.get("data", {})
        if not event_data:
            logger.warning("Empty data for %s", alias)
            results[alias] = {"status": "error", "detail": "Empty data"}
            time.sleep(1)
            continue

        results[alias] = {
            "status": "ok",
            "actual": event_data.get("actual"),
            "forecast": event_data.get("forecast"),
            "previous": event_data.get("previous"),
            "surprise_score": event_data.get("surprise_score"),
            "sentiment_label": event_data.get("sentiment_label"),
            "impact": event_data.get("impact"),
            "release_time": event_data.get("release_time"),
            "published_at": event_data.get("published_at"),
            "title": event_data.get("title"),
            "canonical_id": data.get("canonical_id"),
            "observation_period": data.get("observation_period"),
            "first_print": event_data.get("first_print"),
            "revision_seq": event_data.get("revision_seq"),
            "confidence_note": _assess_timing_confidence(event_data),
            "safety": data.get("safety", {}),
            "_raw": data,
        }
        time.sleep(1)

    return results


def fetch_macro_calendar(
    aliases: Optional[List[str]] = None, days: int = 7
) -> Dict[str, List[dict]]:
    """Fetch upcoming releases for each alias from /v1/macro/calendar."""
    aliases = aliases or DEFAULT_CALENDAR_EVENTS
    url = f"{QUANTGIST_BASE_URL}/v1/macro/calendar"
    data = safe_quantgist_request(url, {"events": ",".join(aliases), "days": days})

    if data is None:
        logger.error("Failed to fetch macro calendar")
        return {}

    result = {}
    for entry in data.get("data", []):
        alias = entry.get("alias")
        if alias:
            result[alias] = entry.get("data", [])
    return result


def load_last_seen() -> dict:
    """Load persisted state used for value-diff dedupe."""
    if os.path.exists(LAST_SEEN_FILE):
        try:
            with open(LAST_SEEN_FILE, "r") as f:
                return json.load(f)
        except Exception:
            logger.warning("Failed to parse %s, starting with empty state", LAST_SEEN_FILE)
            return {}
    return {}


def save_last_seen(state: dict) -> None:
    os.makedirs(os.path.dirname(LAST_SEEN_FILE), exist_ok=True)
    with open(LAST_SEEN_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_should_alert(event_data: dict, last_seen: dict) -> Tuple[bool, dict]:
    """
    Decide whether an event is worth surfacing: first time seen for this
    observation_period, or its actual value changed since last run.

    Dedupe key is canonical_id:observation_period rather than the API's
    first_print/revision_seq flags, since those have been observed to be
    inconsistent with the actual revision history.
    """
    if event_data.get("status") != "ok":
        return True, {"status": "fetch_error", "detail": event_data.get("detail", "unknown")}

    canonical_id = event_data.get("canonical_id") or "unknown"
    obs_period = event_data.get("observation_period") or "unknown"
    actual = event_data.get("actual")
    confidence_note = event_data.get("confidence_note", "unknown")

    if canonical_id == "unknown" or obs_period == "unknown" or actual is None:
        return True, {"status": "incomplete_data", "confidence_note": confidence_note}

    key = f"{canonical_id}:{obs_period}"

    if key not in last_seen:
        last_seen[key] = {"actual": actual, "first_seen_at": datetime.now(timezone.utc).isoformat()}
        return True, {"status": "first_observed", "confidence_note": confidence_note}

    if last_seen[key]["actual"] != actual:
        old_val = last_seen[key]["actual"]
        last_seen[key]["actual"] = actual
        last_seen[key]["last_changed_at"] = datetime.now(timezone.utc).isoformat()
        return True, {
            "status": "value_changed",
            "old_actual": old_val,
            "new_actual": actual,
            "confidence_note": confidence_note,
        }

    return False, {"status": "no_change"}


def fetch_macro_data(
    actual_aliases: Optional[List[str]] = None,
    calendar_aliases: Optional[List[str]] = None,
    calendar_days: int = 7,
) -> Dict:
    """Combine actuals, calendar, and alerts into a single snapshot."""
    actual_aliases = actual_aliases or DEFAULT_ACTUAL_EVENTS
    calendar_aliases = calendar_aliases or DEFAULT_CALENDAR_EVENTS

    logger.info("Fetching actuals...")
    actuals = fetch_macro_actuals(actual_aliases)

    logger.info("Fetching upcoming calendar...")
    calendar = fetch_macro_calendar(calendar_aliases, days=calendar_days)

    last_seen = load_last_seen()
    alerts = []

    for alias, data in actuals.items():
        should_alert, meta = check_should_alert(data, last_seen)
        if should_alert:
            alerts.append({
                "alias": alias,
                "actual": data.get("actual"),
                "forecast": data.get("forecast"),
                "release_time": data.get("release_time"),
                "surprise_score": data.get("surprise_score"),
                **meta,
            })

    save_last_seen(last_seen)

    if alerts:
        logger.info("%d item(s) worth flagging.", len(alerts))

    return {
        "actuals": actuals,
        "calendar": calendar,
        "alerts": alerts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    print("=" * 70)
    print("MACRO FETCHER")
    print("=" * 70)

    result = fetch_macro_data()

    print("\n[ACTUALS]")
    for alias, info in result["actuals"].items():
        if info.get("status") != "ok":
            print(f"   {alias}: status={info.get('status')} - {info.get('detail')}")
        else:
            print(
                f"   {alias}: actual={info['actual']} vs forecast={info['forecast']} "
                f"| confidence={info['confidence_note']} "
                f"| first_print(API, unverified)={info['first_print']}"
            )

    print("\n[CALENDAR]")
    for alias, events in result["calendar"].items():
        for ev in events:
            print(f"   {alias}: {ev.get('release_time')} - {ev.get('title')} (impact={ev.get('impact')})")

    print("\n[ALERTS]")
    tags = {
        "first_observed": "NEW",
        "value_changed": "REVISED",
        "incomplete_data": "INCOMPLETE",
        "fetch_error": "FETCH ERROR",
    }
    for a in result["alerts"]:
        tag = tags.get(a["status"], "INFO")
        print(f"   [{tag}] {a['alias']}: status={a['status']}, confidence={a.get('confidence_note')}")

    print("\nDone.")