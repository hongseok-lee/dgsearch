from types import SimpleNamespace

from scrapy.http import Request, Response

from dgsearch.middlewares import RateLimitMiddleware


class Slot:
    def __init__(self, concurrency=4, delay=1.0):
        self.concurrency = concurrency
        self.delay = delay


def middleware(slot, **kwargs):
    instance = RateLimitMiddleware(**kwargs)
    instance.crawler = SimpleNamespace(
        engine=SimpleNamespace(downloader=SimpleNamespace(slots={"www.daangn.com": slot})),
        spider=SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: None, info=lambda *args: None)),
    )
    return instance


def response(status=200, headers=None):
    request = Request("https://www.daangn.com/test", meta={"download_slot": "www.daangn.com"})
    return request, Response(request.url, status=status, headers=headers, request=request)


def test_429_halves_concurrency_and_adds_backoff():
    slot = Slot(concurrency=8, delay=1)
    instance = middleware(slot)
    request, result = response(429, {"Retry-After": "90"})

    assert instance.process_response(request, result) is result
    assert slot.concurrency == 4
    assert slot.delay == 90


def test_failure_window_reduces_concurrency():
    slot = Slot(concurrency=4, delay=1)
    instance = middleware(
        slot,
        window_size=10,
        min_samples=10,
        failure_rate=0.2,
        recovery_rate=0,
    )
    statuses = [200] * 7 + [500] * 3
    for status in statuses:
        request, result = response(status)
        instance.process_response(request, result)

    assert slot.concurrency == 2
    assert slot.delay == 2


def test_clean_window_recovers_one_step_at_a_time():
    slot = Slot(concurrency=2, delay=1)
    instance = middleware(
        slot,
        max_concurrency=5,
        window_size=10,
        min_samples=5,
        failure_rate=0.2,
        recovery_rate=0,
    )
    for _ in range(10):
        request, result = response(200)
        instance.process_response(request, result)

    assert slot.concurrency == 3
    assert slot.delay == 0.8
