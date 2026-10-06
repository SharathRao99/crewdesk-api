"""Thin GitHub REST client that respects rate limits."""

import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

API = "https://api.github.com"
MAX_NETWORK_RETRIES = 12
SEARCH_DELAY_SECONDS = 2.2  # authenticated search limit is 30 requests/min


class GitHubClient:
    def __init__(self, token: Optional[str] = None):
        token = token or os.getenv("GITHUB_TOKEN")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "talent-sourcer",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            logger.warning("GITHUB_TOKEN not set: expect very low rate limits")
        self._http = httpx.Client(base_url=API, headers=headers, timeout=30)

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, search: bool = False) -> Any:
        """GET with retries. Returns parsed JSON, or None on 404.

        Rate limits are waited out; network errors (offline, DNS, timeouts) are
        retried with backoff for up to ~30 minutes before giving up.
        """
        rate_limited = network_failures = 0
        while True:
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as e:
                network_failures += 1
                if network_failures > MAX_NETWORK_RETRIES:
                    raise
                wait = min(15 * network_failures, 300)
                logger.warning(f"Network error on {path} ({e.__class__.__name__}); retry {network_failures} in {wait}s")
                time.sleep(wait)
                continue
            network_failures = 0
            if search:
                time.sleep(SEARCH_DELAY_SECONDS)

            if response.status_code == 404:
                return None
            if response.status_code in (403, 429) and self._is_rate_limited(response):
                rate_limited += 1
                if rate_limited > 5:
                    raise RuntimeError(f"Gave up on {path} after repeated rate limiting")
                wait = self._wait_seconds(response)
                logger.warning(f"Rate limited, sleeping {wait:.0f}s (attempt {rate_limited})")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if "retry-after" in response.headers:
            return True
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        return "rate limit" in response.text.lower()

    @staticmethod
    def _wait_seconds(response: httpx.Response) -> float:
        if "retry-after" in response.headers:
            return float(response.headers["retry-after"]) + 1
        reset = response.headers.get("x-ratelimit-reset")
        if reset and response.headers.get("x-ratelimit-remaining") == "0":
            return max(float(reset) - time.time(), 1) + 1
        return 60.0
