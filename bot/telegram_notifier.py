"""
Telegram notifier for sending LLM analysis reports.

Uses the Bot API to send messages to a channel/group/chat.
Messages are automatically split into chunks if they exceed Telegram's
4096-character limit.
"""

import logging
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)
TELEGRAM_MAX_LENGTH = 4096


def _split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split text into chunks at newline boundaries, each within max_length."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_length:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_telegram_message(text: str) -> bool:
    """
    Send a text message to the configured Telegram chat.

    Returns True if all chunks were sent successfully, False otherwise.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID not set")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_message(text)
    success = True

    for i, chunk in enumerate(chunks, 1):
        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": chunk,
            # No parse_mode to avoid Markdown/V2 errors from LLM output.
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            logger.info("Telegram chunk %d/%d sent", i, len(chunks))
        except requests.RequestException as e:
            logger.error("Failed to send chunk %d/%d: %s", i, len(chunks), e)
            success = False

    return success


if __name__ == "__main__":
    # Quick test – run with: python -m utils.telegram_notifier
    logging.basicConfig(level=logging.INFO)
    test_msg = (
        "📊 MACRO LIQUIDITY UPDATE — 29 Jul 2026, 15:35 WIB\n\n"
        "🔹 DATA UTAMA\n"
        "- DXY: 101.33 (-0.04%) | US10Y: 4.60% (-0.80%)\n"
        "- Gold: $4096.40 (+1.49%) | BTC: $64223.29 (+0.55%)\n"
        "- S&P 500: 7428.78 (+0.21%) | Nasdaq: 24876.91 (-0.22%)\n"
        "- Crude Oil: $82.19 (+3.70%) | VIX: 18.21 (-2.46%)\n\n"
        "💡 KESIMPULAN: Risk-on rotation continues...\n"
    )
    print("Sending test message...")
    ok = send_telegram_message(test_msg)
    print(f"Result: {'✅ Success' if ok else '❌ Failed'}")