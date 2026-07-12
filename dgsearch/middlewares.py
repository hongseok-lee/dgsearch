from __future__ import annotations

from email.utils import parsedate_to_datetime
from time import time


class RateLimitMiddleware:
    """Slow the whole download slot when the server asks us to back off."""

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        instance.crawler = crawler
        return instance

    def process_response(self, request, response):
        if response.status != 429:
            return response

        retry_after = response.headers.get(b"Retry-After")
        delay = self._retry_after_seconds(retry_after) if retry_after else 120.0
        delay = min(900.0, max(60.0, delay))

        slot_key = request.meta.get("download_slot")
        slot = self.crawler.engine.downloader.slots.get(slot_key) if slot_key else None
        if slot is not None:
            slot.delay = max(slot.delay, delay)

        self.crawler.spider.logger.warning("429 received; download slot delay raised to %.0fs", delay)
        return response

    @staticmethod
    def _retry_after_seconds(value: bytes) -> float:
        text = value.decode("ascii", errors="ignore").strip()
        try:
            return float(text)
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(text).timestamp() - time())
            except (TypeError, ValueError):
                return 120.0
