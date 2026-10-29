"""Rate limiting + retries for the in-house APIs (OCR, split). One limiter per API (and rate), shared by every thread
in the process, so intake and the issue check together never exceed the configured requests per second.

Retry policy per call (config: zoop_api.<api>.retries / backoff_s / max_total_s):
  429            → pause the SHARED limiter for Retry-After (capped at 60 s; odd values → backoff), try again
  5xx / timeout / connection error → exponential backoff (backoff_s, 2×, 4× …), up to `retries` more tries;
                   then the last 5xx response is returned / the last exception is raised for the caller to report
  401 / 403      → ApiAuthError at once, never retried (a bad key must not cause a retry storm)
  anything else  → returned as is
No retry starts once `max_total_s` has passed, so one hanging API can't hold intake for 4 × the timeout.
Every retry is logged, so a degrading API shows up before it fails outright.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque

import requests

from .errors import line
from .settings import Settings

log = logging.getLogger("estamp")
MAX_RETRY_AFTER_S = 60.0


class ApiAuthError(RuntimeError):
    """The API refused the request (401/403). Intake must stop for the cycle, not retry."""


class RateLimiter:
    """Sliding window: at most `rate` calls start inside any 1-second window. A rate below 1 means one call per
    1/rate seconds; a fractional rate above 1 is rounded DOWN (never faster than configured)."""

    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep):
        if not rate or rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        self.max_calls, self.window = (int(rate), 1.0) if rate >= 1 else (1, 1.0 / rate)
        self.clock, self.sleep = clock, sleep
        self.starts: deque[float] = deque()
        self.paused_until = 0.0
        self.lock = threading.Lock()

    def pause(self, seconds: float) -> None:
        """Hold every caller (e.g. after a 429) until `seconds` from now."""
        with self.lock:
            self.paused_until = max(self.paused_until, self.clock() + seconds)

    def acquire(self) -> float:
        """Wait for a slot; return the start time granted. The slot is granted atomically under the lock (so the
        window is never overshot), but the wait/sleep happens WITHOUT the lock – one caller's 429 pause must not
        freeze every other thread (H-4)."""
        while True:
            with self.lock:
                now = self.clock()
                if now >= self.paused_until:
                    while self.starts and now - self.starts[0] >= self.window:
                        self.starts.popleft()
                    if len(self.starts) < self.max_calls:
                        self.starts.append(now)
                        return now
                    wait = self.window - (now - self.starts[0])
                else:
                    wait = self.paused_until - now
            self.sleep(wait)      # lock released – concurrent callers wait in parallel, then re-check


def _retry_after(res, default: float) -> float:
    try:
        v = float(res.headers.get("Retry-After", default))
    except (TypeError, ValueError):      # HTTP-date or garbage
        return default
    return min(v, MAX_RETRY_AFTER_S) if v > 0 else default     # catches 0, negatives and nan


def _body(res) -> str:
    return (getattr(res, "content", b"") or b"")[:200].decode("utf-8", "replace")


def call_with_retry(fn, limiter: RateLimiter, retries: int = 3, backoff_s: float = 1.0, sleep=time.sleep,
                    clock=time.monotonic, name: str = "API", max_total_s: float | None = None):
    """fn() performs one HTTP request and returns the response. See the module docstring for the policy."""
    start = clock()
    for attempt in range(retries + 1):
        limiter.acquire()
        wait = backoff_s * (2 ** attempt)
        try:
            res = fn()
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == retries or (max_total_s is not None and clock() - start >= max_total_s):
                raise
            log.warning(f"{name}: {type(e).__name__} – retry {attempt + 1}/{retries} in {wait:.0f} s")
            sleep(wait)
            continue
        if res.status_code in (401, 403):
            raise ApiAuthError(f"{name} refused the request (HTTP {res.status_code}): {_body(res)}")
        if res.status_code == 429 or res.status_code >= 500:
            if attempt == retries or (max_total_s is not None and clock() - start >= max_total_s):
                return res
            msg = f"{name}: HTTP {res.status_code} – retry {attempt + 1}/{retries} in {wait:.0f} s"
            if res.status_code == 429:
                wait = _retry_after(res, wait)
                limiter.pause(wait)
                msg = line("E112", f"{name}: HTTP 429 – pausing {wait:.0f} s, retry {attempt + 1}/{retries}")
            log.warning(msg)
            sleep(wait)
            continue
        return res
    raise AssertionError("unreachable")


_limiters: dict[tuple[str, float], RateLimiter] = {}
_limiters_lock = threading.Lock()
_sleep = time.sleep          # backoff sleep; tests replace it


def limiter_for(settings: Settings, api: str) -> RateLimiter:
    """The process-wide limiter for `api` ('ocr' or 'split') at its configured zoop_api.<api>.rate_per_s."""
    rate = float(settings.get(f"zoop_api.{api}.rate_per_s", 5))
    with _limiters_lock:
        if (api, rate) not in _limiters:
            _limiters[(api, rate)] = RateLimiter(rate)
        return _limiters[(api, rate)]


def api_call(settings: Settings, api: str, fn):
    """One rate-limited, retried call to `api` with the retry settings from config."""
    total = settings.get(f"zoop_api.{api}.max_total_s")
    return call_with_retry(fn, limiter_for(settings, api), retries=int(settings.get(f"zoop_api.{api}.retries", 3)),
                           backoff_s=float(settings.get(f"zoop_api.{api}.backoff_s", 1.0)), sleep=_sleep,
                           name=f"{api} API", max_total_s=float(total) if total else None)
