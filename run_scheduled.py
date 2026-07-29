"""
Scheduled entry point for production runs.
Called by cron/systemd timer to refresh data and send analysis to Telegram.
"""

import logging
from utils.logging_config import setup_logging
from fetcher.orchestrator import main as full_refresh
from brain.pipeline import run_analysis
from bot.telegram_notifier import send_telegram_message

# Setup logging ONCE at the very beginning
setup_logging()

logger = logging.getLogger(__name__)


def run_scheduled_cycle() -> None:
    """Full cycle: refresh snapshot → run analysis → send to Telegram."""
    logger.info("=" * 60)
    logger.info("Starting scheduled macro liquidity cycle...")

    # 1. Refresh snapshot (market + macro + news)
    logger.info("Step 1: Refreshing full snapshot...")
    try:
        full_refresh()
    except Exception as e:
        logger.error("Snapshot refresh failed: %s", e, exc_info=True)
        return

    # 2. Run AI analysis
    logger.info("Step 2: Running AI analysis...")
    result = run_analysis(refresh_first=False)
    if not result or result.startswith("❌"):
        logger.error("Analysis failed: %s", result)
        return

    # 3. Send to Telegram
    logger.info("Step 3: Sending to Telegram...")
    success = send_telegram_message(result)
    if success:
        logger.info("✅ Cycle completed successfully.")
    else:
        logger.error("❌ Telegram send failed, but analysis saved to file.")

    logger.info("=" * 60)


if __name__ == "__main__":
    run_scheduled_cycle()