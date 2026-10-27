"""A-07: rate limiter + retry policy shared by the OCR and split API adapters (no network, no DB)."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from estamp.http_api import ApiAuthError, RateLimiter, call_with_retry  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


class Resp:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}


def _max_in_any_window(times, window=1.0):
    times = sorted(times)
    return max(sum(1 for u in times if t <= u < t + window) for t in times)


def test_limiter_never_exceeds_rate_in_any_one_second_window():
    clock = FakeClock()
    lim = RateLimiter(5, clock=clock.time, sleep=clock.sleep)
    stamps = []
    for _ in range(50):
        lim.acquire()
        stamps.append(clock.now)
    assert _max_in_any_window(stamps) == 5
    assert stamps[-1] == pytest.approx(9.0)          # 50 calls at 5/s: last one starts after 9 s, not later


def test_limiter_is_shared_by_parallel_threads():
    """100 threads hit one limiter at once (real clock, 50/s so the test stays ~1 s). acquire() returns the start
    time it granted, so the check doesn't depend on thread scheduling after the grant."""
    lim, stamps, lock = RateLimiter(50), [], threading.Lock()

    def worker():
        t = lim.acquire()
        with lock:
            stamps.append(t)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(stamps) == 100 and _max_in_any_window(stamps) <= 50


def test_fractional_rate_spaces_calls_out():
    clock = FakeClock()
    lim = RateLimiter(0.5, clock=clock.time, sleep=clock.sleep)
    stamps = [lim.acquire() for _ in range(3)]
    assert stamps == [0.0, 2.0, 4.0]


@pytest.mark.parametrize("rate", [0, -1])
def test_rate_must_be_positive(rate):
    with pytest.raises(ValueError):
        RateLimiter(rate)


def test_pause_holds_every_caller():
    """A 429 pauses the shared limiter, so other threads slow down too – not only the one that got the 429."""
    clock = FakeClock()
    lim = RateLimiter(100, clock=clock.time, sleep=clock.sleep)
    lim.pause(10)
    assert lim.acquire() >= 10


def _run(replies, retries=3, call_s=0.0, max_total_s=None, lim=None):
    """replies: list of Resp or exceptions, consumed one per attempt; each attempt takes call_s seconds."""
    clock, attempts = FakeClock(), []

    def fn():
        attempts.append(1)
        clock.now += call_s
        r = replies[len(attempts) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    out = call_with_retry(fn, lim or RateLimiter(1000, clock=clock.time, sleep=clock.sleep), retries=retries,
                          backoff_s=1.0, sleep=clock.sleep, clock=clock.time, name="split API",
                          max_total_s=max_total_s)
    return out, len(attempts), clock.slept


def test_429_waits_for_retry_after_then_succeeds():
    out, n, slept = _run([Resp(429, {"Retry-After": "7"}), Resp(200)])
    assert out.status_code == 200 and n == 2 and 7 in slept


def test_5xx_backs_off_exponentially_then_succeeds():
    out, n, slept = _run([Resp(500), Resp(502), Resp(200)])
    assert out.status_code == 200 and n == 3 and slept == [1.0, 2.0]


def test_5xx_until_retries_run_out_returns_the_last_response():
    out, n, _ = _run([Resp(503)] * 4, retries=3)
    assert out.status_code == 503 and n == 4


def test_timeouts_retry_then_raise_the_last_error():
    with pytest.raises(requests.Timeout):
        _run([requests.Timeout()] * 4, retries=3)


def test_connection_error_recovers_on_retry():
    out, n, _ = _run([requests.ConnectionError(), Resp(200)])
    assert out.status_code == 200 and n == 2


@pytest.mark.parametrize("retry_after,expected", [("3600", 60.0), ("-1", 1.0), ("inf", 60.0), ("nan", 1.0),
                                                   ("Wed, 21 Oct 2026 07:28:00 GMT", 1.0)])
def test_retry_after_is_clamped(retry_after, expected):
    """Never sleep an hour, never crash on a negative/odd value: capped at 60 s, else the normal backoff."""
    out, _, slept = _run([Resp(429, {"Retry-After": retry_after}), Resp(200)])
    assert out.status_code == 200 and slept[0] == expected


def test_total_time_cap_stops_retrying_a_hanging_api():
    """Each attempt hangs for the full timeout; retries stop once the total cap is reached (not 4 × 300 s)."""
    with pytest.raises(requests.Timeout):
        _run([requests.Timeout()] * 4, call_s=300, max_total_s=400)
    _, n, _ = _run([requests.Timeout(), Resp(200)], call_s=300, max_total_s=400)
    assert n == 2
    with pytest.raises(requests.Timeout):
        _run([requests.Timeout(), requests.Timeout(), Resp(200)], call_s=300, max_total_s=400)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_never_retried(status):
    with pytest.raises(ApiAuthError, match=f"split API.*HTTP {status}"):
        _run([Resp(status), Resp(200)])


def test_auth_failure_keeps_the_server_message():
    r = Resp(403)
    r.content = b'{"message":"blocked by WAF"}'
    with pytest.raises(ApiAuthError, match="blocked by WAF"):
        _run([r])


def test_429_pauses_the_shared_limiter():
    clock = FakeClock()
    lim = RateLimiter(1000, clock=clock.time, sleep=clock.sleep)
    _run([Resp(429, {"Retry-After": "7"}), Resp(200)], lim=lim)
    assert lim.paused_until >= 7


def test_limiter_for_keeps_one_limiter_per_api_and_rate():
    from estamp.http_api import _limiters, limiter_for

    class S:
        def __init__(self, rate):
            self.rate = rate

        def get(self, key, default=None):
            return self.rate if key.endswith("rate_per_s") else default

    _limiters.clear()
    a, b, c = limiter_for(S(5), "split"), limiter_for(S(5), "split"), limiter_for(S(2), "split")
    assert a is b and a is not c and limiter_for(S(5), "ocr") is not a


def test_4xx_other_than_auth_and_429_is_returned_without_retry():
    out, n, _ = _run([Resp(400), Resp(200)])
    assert out.status_code == 400 and n == 1
