"""
Safe wrapper for QuantGist API requests with automatic key rotation
and graceful error handling.
"""

import time
import logging
import requests
from config import QUANTGIST_API_KEYS
from utils.api_key_manager import APIKeyRotator

logger = logging.getLogger(__name__)
quantgist_rotator = APIKeyRotator(QUANTGIST_API_KEYS, service_name="QuantGist")


def safe_quantgist_request(url, params, retries: int = 3, timeout: int = 10):
    """Send a GET request to QuantGist API with key rotation on failures."""
    for attempt in range(retries):
        try:
            key = quantgist_rotator.get_key()
            resp = requests.get(
                url,
                headers={"X-API-Key": key},
                params=params,
                timeout=timeout,
            )

            if resp.status_code == 429:
                quantgist_rotator.rotate("rate limit")
                continue
            if resp.status_code in (401, 403):
                quantgist_rotator.rotate("auth error")
                continue
            if resp.status_code == 402:
                logger.warning("402 Payment Required for %s (params=%s) — upgrade plan needed", url, params)
                return None
            if resp.status_code == 404:
                return None

            resp.raise_for_status()
            return resp.json()

        except RuntimeError as e:
            logger.error("All keys blocked: %s", e)
            time.sleep(5)
        except requests.RequestException as e:
            logger.warning("Request error (attempt %d/%d): %s", attempt + 1, retries, e)
            time.sleep(2)

    logger.error("All %d retries exhausted for %s", retries, url)
    return None