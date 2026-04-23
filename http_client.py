"""Shared HTTP client with retry, rate limiting, and Retry-After support.

Used by :mod:`edgar_fetcher` (SEC JSON) and :mod:`finance_model_v2` (yfinance
DataFrame). Collapses the two drifted one-shot fetch paths into a single
primitive with a documented retry contract (see ``ITERATION_PLAN.md`` Round 4).

Retry contract:
    * Exponential backoff: base delay 1s, multiplier 2, max 5 attempts,
      jitter 0-1s (matches ``finance_model_v2._download_batch``).
    * HTTP 429 honors ``Retry-After`` (integer seconds or HTTP-date per
      RFC 7231 section 7.1.3); falls through to exponential backoff if absent.
    * HTTP 5xx retries with exponential backoff.
    * HTTP 4xx except 429 is raised immediately as the original
      :class:`urllib.error.HTTPError` - callers (notably
      ``edgar_fetcher.fetch_one``) rely on the original exception type and
      status code for 404 handling.
    * In :func:`retrying_df_fetch`, an empty DataFrame is treated as a 429
      (yfinance's 429 surfaces as an empty frame, not an exception).
    * After ``max_retries`` exhausted, raises :class:`HttpRetryExhausted` so
      callers can surface a visible skip count rather than silently drop data.
"""
from __future__ import annotations

import email.utils
import json
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

import pandas as pd


# Backoff shape — mirrors finance_model_v2._download_batch at lines 240-247.
_BACKOFF_BASE = 2.0
_BACKOFF_INITIAL = 1.0
_JITTER_MAX = 1.0


class HttpRetryExhausted(Exception):
    """Raised when max_retries is exhausted. Callers should log at WARN and
    surface a visible skip count (see C-5 in audit-findings.md)."""


class TokenBucket:
    """Thread-safe token bucket rate limiter.

    ``acquire()`` blocks until a token is available. Refills at
    ``rate_per_sec`` tokens per second up to ``capacity``. Uses
    :func:`time.monotonic` so wall-clock jumps don't skew the bucket.
    """

    def __init__(self, rate_per_sec: float, capacity: int) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.rate_per_sec = float(rate_per_sec)
        self.capacity = int(capacity)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                if elapsed > 0:
                    self._tokens = min(
                        self.capacity,
                        self._tokens + elapsed * self.rate_per_sec,
                    )
                    self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Sleep only as long as needed for the next token to arrive.
                deficit = 1.0 - self._tokens
                wait = deficit / self.rate_per_sec
            # Release lock while sleeping so other threads can still refill
            # their view of the clock on wakeup.
            time.sleep(wait)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse an HTTP Retry-After header into seconds.

    Handles both forms from RFC 7231 section 7.1.3:
        * delta-seconds (e.g. ``"120"``)
        * HTTP-date (e.g. ``"Fri, 31 Dec 1999 23:59:59 GMT"``)

    Returns None if the header is missing or unparseable.
    """
    if not value:
        return None
    value = value.strip()
    # Try integer seconds first.
    try:
        secs = float(value)
        return max(0.0, secs)
    except ValueError:
        pass
    # Fall back to HTTP-date.
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    now = time.time()
    target = dt.timestamp()
    return max(0.0, target - now)


def _sleep_for_attempt(attempt: int, retry_after: Optional[float] = None) -> None:
    """Sleep for the backoff interval of ``attempt`` (1-indexed).

    If ``retry_after`` is provided, honor it instead of exponential backoff.
    Jitter (0-1s) is always added on top.
    """
    if retry_after is not None:
        delay = retry_after + random.uniform(0, _JITTER_MAX)
    else:
        # attempt=1 -> 1s, attempt=2 -> 2s, attempt=3 -> 4s, ... (base^(n-1))
        delay = _BACKOFF_INITIAL * (_BACKOFF_BASE ** (attempt - 1)) + random.uniform(0, _JITTER_MAX)
    time.sleep(delay)


def get_json(
    url: str,
    *,
    headers: dict,
    timeout: float = 30.0,
    max_retries: int = 5,
    rate_limiter: Optional[TokenBucket] = None,
) -> dict:
    """GET ``url`` and return the parsed JSON body with retry/rate-limit support."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        if rate_limiter is not None:
            rate_limiter.acquire()
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body.decode("utf-8"))
        # Ordering is load-bearing: HTTPError is a subclass of URLError, so it
        # must be caught first or all HTTP errors would collapse into the
        # URLError branch and lose their status-code-specific handling.
        except urllib.error.HTTPError as e:
            code = e.code
            if code == 429:
                retry_after = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
                last_exc = e
                if attempt >= max_retries:
                    break
                _sleep_for_attempt(attempt, retry_after=retry_after)
                continue
            if 500 <= code < 600:
                last_exc = e
                if attempt >= max_retries:
                    break
                _sleep_for_attempt(attempt)
                continue
            # 4xx other than 429: permanent. Re-raise the original HTTPError so
            # edgar_fetcher.fetch_one's 404 branch (line 277) still works.
            raise
        except urllib.error.URLError as e:
            # Network-level failure (DNS, connection refused, timeout). Retry.
            last_exc = e
            if attempt >= max_retries:
                break
            _sleep_for_attempt(attempt)
            continue
    raise HttpRetryExhausted(
        f"GET {url} failed after {max_retries} attempts: {last_exc!r}"
    )


def retrying_df_fetch(
    fetch_fn: Callable[[], "pd.DataFrame"],
    *,
    max_retries: int = 3,
    rate_limiter: Optional[TokenBucket] = None,
) -> "pd.DataFrame":
    """Call ``fetch_fn`` (typically a ``lambda: yf.download(...)``) with retry.

    Empty DataFrames are treated as retryable — yfinance's 429 surfaces as an
    empty frame rather than an exception. Exceptions from ``fetch_fn`` are
    also retried. After ``max_retries`` exhausted, raises
    :class:`HttpRetryExhausted`.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        if rate_limiter is not None:
            rate_limiter.acquire()
        try:
            df = fetch_fn()
        except Exception as e:  # noqa: BLE001 - yfinance raises many types
            last_exc = e
            if attempt >= max_retries:
                break
            _sleep_for_attempt(attempt)
            continue
        if df is None or (hasattr(df, "empty") and df.empty):
            last_exc = HttpRetryExhausted(f"empty DataFrame on attempt {attempt}")
            if attempt >= max_retries:
                break
            _sleep_for_attempt(attempt)
            continue
        return df
    raise HttpRetryExhausted(
        f"DataFrame fetch failed after {max_retries} attempts: {last_exc!r}"
    )
