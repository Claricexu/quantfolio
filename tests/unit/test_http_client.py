"""Unit tests for http_client — token bucket, retry, Retry-After, 4xx/5xx.

Plain-assert style, no pytest. Runs via ``python tests/unit/run_all.py``.
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.error
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pandas as pd

import http_client
from http_client import (
    HttpRetryExhausted,
    TokenBucket,
    get_json,
    retrying_df_fetch,
)


# --- helpers -----------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for urllib's HTTPResponse, usable as a context manager."""

    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    """Build a realistic HTTPError matching what urlopen raises on non-2xx."""
    return urllib.error.HTTPError(
        url="http://example.test/",
        code=code,
        msg=f"HTTP {code}",
        hdrs=headers or {},
        fp=io.BytesIO(b""),
    )


def _silence_sleep(monkey_sleeps: list[float]):
    """Return a patched time.sleep that records the requested delay and returns
    immediately. Used to keep the test fast while asserting sleep-schedule."""

    def fake_sleep(s):
        monkey_sleeps.append(float(s))

    return fake_sleep


# --- tests -------------------------------------------------------------------

def test_token_bucket_rate_limiter_math() -> None:
    """TokenBucket(rate=10, cap=10): first 10 acquires are from the initial
    burst capacity (~instant); the next 10 must wait for refill at 10/s, so
    calls 11-20 take ~1.0s total. With a small timing slack for scheduler
    overhead we assert >= 0.8s and < 2.5s (real expected value ~1.0s)."""
    bucket = TokenBucket(rate_per_sec=10, capacity=10)
    start = time.monotonic()
    for _ in range(20):
        bucket.acquire()
    elapsed = time.monotonic() - start
    # Expected: burst of 10 (instant) + 10 more at 10/s = ~1.0s.
    # Brief's phrasing ("≥ 1.8s") targeted a no-burst interpretation; the
    # constructor explicitly takes a capacity for initial burst, so 1.0s is
    # the honest expected value. Lower bound: 0.8s to leave slack. Upper
    # bound: 2.5s to catch pathological over-throttling (C-4 mitigation).
    assert elapsed >= 0.8, f"bucket drained too fast: {elapsed:.3f}s"
    assert elapsed < 2.5, f"bucket over-throttled: {elapsed:.3f}s"


def test_token_bucket_concurrent_two_threads_respect_rate() -> None:
    """Two threads sharing a single TokenBucket(rate=10, cap=1) each acquire
    20 tokens. Total 40 acquires at 10/s = ~4.0s steady state (cap=1 means
    near-zero initial burst). Assert (a) elapsed >= 3.8s so we know the
    shared limiter actually serialized both threads against the same rate,
    and (b) both threads completed all 20 acquires (neither starved).

    Pins the contract for api_server.py:734's ThreadPoolExecutor(max_workers=2)
    Lite/Pro parallel backtest path: once Phase 4.2 ships a shared yfinance
    TokenBucket, this test guards against the lock-release-before-sleep race
    regressing on a Yahoo-flap Friday night."""
    bucket = TokenBucket(rate_per_sec=10, capacity=1)
    counts = [0, 0]

    def worker(idx: int) -> None:
        for _ in range(20):
            bucket.acquire()
            counts[idx] += 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    start = time.monotonic()
    for t in threads:
        t.start()
    # Generous join timeout: expected ~4.0s, so 30s catches a hung limiter
    # without making the test itself a source of CI flakes.
    for t in threads:
        t.join(timeout=30.0)
    elapsed = time.monotonic() - start

    assert all(not t.is_alive() for t in threads), \
        f"thread(s) hung past 30s join timeout; counts={counts}"
    assert counts == [20, 20], \
        f"expected both threads to complete 20 acquires, got {counts}"
    # 40 acquires at 10/s with capacity=1 should take ~4.0s; 3.8s lower bound
    # leaves ~5% timing slack for the capacity=1 initial token.
    assert elapsed >= 3.8, \
        f"shared limiter did not serialize threads: elapsed={elapsed:.3f}s"
    # Upper bound guards against pathological over-throttling under contention.
    assert elapsed < 7.0, \
        f"shared limiter over-throttled under contention: elapsed={elapsed:.3f}s"


def test_retry_on_429_with_retry_after() -> None:
    """HTTP 429 with ``Retry-After: 2`` should delay ~2s then succeed on 200."""
    success_body = json.dumps({"ok": True}).encode("utf-8")
    responses = [
        _http_error(429, headers={"Retry-After": "2"}),
        _FakeResponse(success_body, status=200),
    ]

    def side_effect(req, timeout):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    sleeps: list[float] = []
    with patch("http_client.urllib.request.urlopen", side_effect=side_effect), \
            patch("http_client.time.sleep", side_effect=_silence_sleep(sleeps)):
        result = get_json("http://example.test/", headers={}, max_retries=3)

    assert result == {"ok": True}
    assert len(sleeps) == 1, f"expected exactly one sleep, got {sleeps}"
    # Retry-After: 2 + jitter up to 1s, so sleep should be in [2.0, 3.0].
    assert 2.0 <= sleeps[0] <= 3.0, f"expected ~2s sleep, got {sleeps[0]:.3f}s"


def test_retry_on_429_without_retry_after() -> None:
    """HTTP 429 without Retry-After should fall through to exponential backoff."""
    success_body = json.dumps({"data": 42}).encode("utf-8")
    responses = [
        _http_error(429, headers={}),  # no Retry-After
        _FakeResponse(success_body, status=200),
    ]

    def side_effect(req, timeout):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    sleeps: list[float] = []
    with patch("http_client.urllib.request.urlopen", side_effect=side_effect), \
            patch("http_client.time.sleep", side_effect=_silence_sleep(sleeps)):
        result = get_json("http://example.test/", headers={}, max_retries=3)

    assert result == {"data": 42}
    # Exponential: attempt 1 failure -> sleep 1.0s + jitter [0,1] = [1.0, 2.0].
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 2.0, f"expected ~1s exp backoff, got {sleeps[0]:.3f}s"


def test_404_raises_immediately() -> None:
    """HTTP 404 must surface as urllib.error.HTTPError on the first call
    (not HttpRetryExhausted). Pins the contract that
    edgar_fetcher.fetch_one's 404 branch (line 277) still works."""
    call_count = {"n": 0}

    def side_effect(req, timeout):
        call_count["n"] += 1
        raise _http_error(404)

    raised = None
    with patch("http_client.urllib.request.urlopen", side_effect=side_effect), \
            patch("http_client.time.sleep", side_effect=_silence_sleep([])):
        try:
            get_json("http://example.test/", headers={}, max_retries=5)
        except urllib.error.HTTPError as e:
            raised = e
        except HttpRetryExhausted as e:
            raised = ("wrapped", e)

    assert isinstance(raised, urllib.error.HTTPError), \
        f"expected urllib.error.HTTPError, got {type(raised).__name__}: {raised!r}"
    assert raised.code == 404
    assert call_count["n"] == 1, f"404 should not retry, got {call_count['n']} calls"


def test_5xx_retries() -> None:
    """Three 500s followed by a 200 should retry and succeed."""
    success_body = json.dumps({"ok": 1}).encode("utf-8")
    responses = [
        _http_error(500),
        _http_error(503),
        _http_error(502),
        _FakeResponse(success_body, status=200),
    ]

    def side_effect(req, timeout):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    sleeps: list[float] = []
    with patch("http_client.urllib.request.urlopen", side_effect=side_effect), \
            patch("http_client.time.sleep", side_effect=_silence_sleep(sleeps)):
        result = get_json("http://example.test/", headers={}, max_retries=5)

    assert result == {"ok": 1}
    # 3 failures -> 3 sleeps.
    assert len(sleeps) == 3, f"expected 3 sleeps, got {len(sleeps)}"


def test_empty_dataframe_retries_then_raises() -> None:
    """With retry_on_empty=True, always-empty fetch_fn should exhaust retries
    and raise HttpRetryExhausted."""
    call_count = {"n": 0}

    def fetch_fn():
        call_count["n"] += 1
        return pd.DataFrame()  # empty

    sleeps: list[float] = []
    raised = None
    with patch("http_client.time.sleep", side_effect=_silence_sleep(sleeps)):
        try:
            retrying_df_fetch(fetch_fn, max_retries=3)
        except HttpRetryExhausted as e:
            raised = e

    assert raised is not None, "expected HttpRetryExhausted"
    assert call_count["n"] == 3, f"expected 3 attempts, got {call_count['n']}"
    # 3 attempts -> 2 sleeps between them (last failure doesn't sleep).
    assert len(sleeps) == 2, f"expected 2 sleeps, got {len(sleeps)}"


# --- runner ------------------------------------------------------------------

def run_all() -> int:
    failed = 0
    for test in (
        test_token_bucket_rate_limiter_math,
        test_token_bucket_concurrent_two_threads_respect_rate,
        test_retry_on_429_with_retry_after,
        test_retry_on_429_without_retry_after,
        test_404_raises_immediately,
        test_5xx_retries,
        test_empty_dataframe_retries_then_raises,
    ):
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    return failed


if __name__ == "__main__":
    print("test_http_client")
    rc = run_all()
    sys.exit(1 if rc else 0)
