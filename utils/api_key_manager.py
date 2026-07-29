"""
Manages rotation of multiple API keys with cooldown until next UTC midnight.
"""

import time
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


class APIKeyRotator:
    def __init__(self, keys: list[str], service_name: str = "API"):
        if not keys:
            raise ValueError(f"[{service_name}] No API keys configured.")
        self.keys = keys
        self.service_name = service_name
        self.index = 0
        self.blocked_until = {}  # key -> timestamp (UTC)

    def get_key(self) -> str:
        """Return the first non-blocked key, or raise if all are blocked."""
        now = time.time()
        n = len(self.keys)

        for _ in range(n):
            key = self.keys[self.index]
            if self.blocked_until.get(key, 0) <= now:
                return key
            self.index = (self.index + 1) % n

        # All keys blocked — find the soonest available
        soonest_key = min(self.keys, key=lambda k: self.blocked_until.get(k, 0))
        wait = max(0, self.blocked_until[soonest_key] - now)
        raise RuntimeError(
            f"[{self.service_name}] All keys blocked. "
            f"Next available at {datetime.fromtimestamp(soonest_key).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

    def rotate(self, reason: str = "rate_limited"):
        """Mark the current key as blocked until next UTC midnight."""
        current_key = self.keys[self.index]
        block_until = self._get_next_utc_reset()
        self.blocked_until[current_key] = block_until

        masked = current_key[:6] + "..." + current_key[-4:]
        reset_time = datetime.fromtimestamp(block_until, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.info("%s key %s %s, blocked until %s", self.service_name, masked, reason, reset_time)

        self.index = (self.index + 1) % len(self.keys)

    @staticmethod
    def _get_next_utc_reset() -> float:
        """Timestamp of next midnight (00:00 UTC)."""
        now = datetime.now(timezone.utc)
        next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return next_reset.timestamp()

    def key_count(self) -> int:
        return len(self.keys)