from __future__ import annotations

from collections import defaultdict, deque
from email.utils import parsedate_to_datetime
from time import time


class RateLimitMiddleware:
    """Adapt slot concurrency from recent server responses."""

    FAILURE_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        max_concurrency=8,
        window_size=40,
        min_samples=20,
        failure_rate=0.15,
        recovery_rate=0.025,
    ):
        self.max_concurrency = max_concurrency
        self.window_size = window_size
        self.min_samples = min_samples
        self.failure_rate = failure_rate
        self.recovery_rate = recovery_rate
        self.windows = defaultdict(lambda: deque(maxlen=self.window_size))

    @classmethod
    def from_crawler(cls, crawler):
        instance = cls(
            max_concurrency=crawler.settings.getint("ADAPTIVE_CONCURRENCY_MAX", 8),
            window_size=crawler.settings.getint("ADAPTIVE_CONCURRENCY_WINDOW", 40),
            min_samples=crawler.settings.getint("ADAPTIVE_CONCURRENCY_MIN_SAMPLES", 20),
            failure_rate=crawler.settings.getfloat("ADAPTIVE_CONCURRENCY_FAILURE_RATE", 0.15),
            recovery_rate=crawler.settings.getfloat("ADAPTIVE_CONCURRENCY_RECOVERY_RATE", 0.025),
        )
        instance.crawler = crawler
        return instance

    def process_response(self, request, response):
        if "cached" in response.flags:
            return response

        slot_key = request.meta.get("download_slot")
        slot = self.crawler.engine.downloader.slots.get(slot_key) if slot_key else None
        if slot is None:
            return response

        window = self.windows[slot_key]
        failed = response.status in self.FAILURE_STATUSES or response.status >= 500
        window.append(failed)

        if response.status == 429:
            retry_after = response.headers.get(b"Retry-After")
            delay = self._retry_after_seconds(retry_after) if retry_after else 120.0
            slot.delay = max(slot.delay, min(900.0, max(60.0, delay)))
            self._decrease(slot, slot_key, "429 rate limit")
            window.clear()
            return response

        if len(window) < self.min_samples:
            return response

        rate = sum(window) / len(window)
        if rate >= self.failure_rate:
            slot.delay = min(120.0, max(2.0, slot.delay * 2))
            self._decrease(slot, slot_key, f"{rate:.0%} recent failures")
            window.clear()
        elif len(window) == self.window_size and rate <= self.recovery_rate:
            self._increase(slot, slot_key, rate)
            window.clear()
        return response

    def _decrease(self, slot, slot_key, reason):
        previous = slot.concurrency
        slot.concurrency = max(1, previous // 2)
        self.crawler.spider.logger.warning(
            "adaptive concurrency %s: %d -> %d (%s), delay=%.1fs",
            slot_key,
            previous,
            slot.concurrency,
            reason,
            slot.delay,
        )

    def _increase(self, slot, slot_key, rate):
        previous = slot.concurrency
        slot.concurrency = min(self.max_concurrency, previous + 1)
        slot.delay = max(0.25, slot.delay * 0.8)
        self.crawler.spider.logger.info(
            "adaptive concurrency %s: %d -> %d (failure rate %.1f%%), delay=%.1fs",
            slot_key,
            previous,
            slot.concurrency,
            rate * 100,
            slot.delay,
        )

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
