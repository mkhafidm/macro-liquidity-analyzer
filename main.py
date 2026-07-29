"""
Telegram bot entry point.
Listens for /analyze commands and responds with the latest macro analysis.
"""

import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from utils.logging_config import setup_logging
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_ID, TELEGRAM_CHAT_ID
from brain.pipeline import run_analysis
from fetcher.orchestrator import refresh_market_only
from bot.telegram_notifier import _split_message

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)


def _get_allowed_ids() -> list[str]:
    """Return whitelisted chat IDs from config."""
    allowed = []
    if TELEGRAM_GROUP_ID:
        allowed.append(str(TELEGRAM_GROUP_ID))
    if TELEGRAM_CHAT_ID:
        allowed.append(str(TELEGRAM_CHAT_ID))
    return allowed


async def analyze_command(update: Update, context) -> None:
    """Handle /analyze command: refresh market data and send analysis."""
    chat_id = str(update.effective_chat.id)
    user = update.effective_user.first_name
    logger.info("Command from chat_id=%s, user=%s", chat_id, user)

    # Security check
    allowed = _get_allowed_ids()
    if allowed and chat_id not in allowed:
        logger.warning("Access denied for chat_id=%s", chat_id)
        await update.message.reply_text("⛔ Access denied.")
        return

    status_msg = await update.message.reply_text("🧠 Processing...")

    try:
        # Refresh market prices only (macro/news stay cached)
        refresh_market_only()

        # Run analysis
        result = run_analysis(refresh_first=False)

        if not result or result.startswith("❌"):
            await status_msg.edit_text(result or "❌ Analysis failed.")
            return

        # Split and send
        chunks = _split_message(result)
        await status_msg.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await update.message.reply_text(chunk)

    except Exception as e:
        logger.error("Error in analyze_command: %s", e, exc_info=True)
        await status_msg.edit_text(f"❌ Error: {e}")


def main() -> None:
    """Start the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set in config.py")
        return

    logger.info("🤖 Macro Liquidity Bot starting...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.run_polling()


if __name__ == "__main__":
    main()