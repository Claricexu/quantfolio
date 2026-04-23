"""Unit tests for edgar_fetcher's http_get_json wrapper (Phase 4.1 of C-4).

Pins the contract that:
    1. http_get_json routes through the module-level SEC_BUCKET TokenBucket.
    2. 429s are retried via http_client.get_json's backoff path.
    3. 404s still propagate as urllib.error.HTTPError on the first call so
       fetch_one's ticker-not-in-taxonomy branch keeps working.

Plain-assert style, no pytest. Runs via ``python tests/unit/run_all.py``.
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import edgar_fetcher
import http_client


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


class _RecordingBucket:
    """TokenBucket stand-in that records every ``acquire()`` call.

    Looks like a TokenBucket to http_client.get_json (only .acquire() is used)
    but doesn't actually sleep or rate-limit — lets us assert the wiring
    without slowing the test down."""

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


def _silence_sleep(monkey_sleeps: list[float]):
    """Return a patched time.sleep that records the delay and returns immediately."""

    def fake_sleep(s: float) -> None:
        monkey_sleeps.append(float(s))

    return fake_sleep


# --- tests -------------------------------------------------------------------

def test_http_get_json_uses_token_bucket() -> None:
    """edgar_fetcher.http_get_json must route through SEC_BUCKET so every SEC
    call is paced by the shared token bucket (closes the ``no rate limiter
    on the SEC path`` gap flagged in Phase 4.0 review)."""
    success_body = json.dumps({"ok": True, "endpoint": "tickers"}).encode("utf-8")

    def side_effect(req, timeout):
        return _FakeResponse(success_body, status=200)

    recording = _RecordingBucket()
    with patch.object(edgar_fetcher, "SEC_BUCKET", recording), \
            patch("http_client.urllib.request.urlopen", side_effect=side_effect):
        result = edgar_fetcher.http_get_json("http://example.test/tickers.json")

    assert result == {"ok": True, "endpoint": "tickers"}
    assert recording.calls == 1, \
        f"SEC_BUCKET.acquire() should be called exactly once per GET, got {recording.calls}"


def test_http_get_json_retries_on_429() -> None:
    """Two 429s followed by a 200 should retry and succeed — proves the
    C-4 retry contract is live on the SEC path (pre-Phase-4.1, SEC calls
    had no retry at all and would surface 429s as bare exceptions)."""
    success_body = json.dumps({"facts": {"us-gaap": {}}}).encode("utf-8")
    responses = [
        _http_error(429, headers={"Retry-After": "1"}),
        _http_error(429, headers={"Retry-After": "1"}),
        _FakeResponse(success_body, status=200),
    ]

    call_count = {"n": 0}

    def side_effect(req, timeout):
        call_count["n"] += 1
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    sleeps: list[float] = []
    # Swap SEC_BUCKET for a recording stub so we don't actually pace 8/s here,
    # and silence http_client.time.sleep so the Retry-After delay returns fast.
    with patch.object(edgar_fetcher, "SEC_BUCKET", _RecordingBucket()), \
            patch("http_client.urllib.request.urlopen", side_effect=side_effect), \
            patch("http_client.time.sleep", side_effect=_silence_sleep(sleeps)):
        result = edgar_fetcher.http_get_json("http://example.test/facts.json")

    assert result == {"facts": {"us-gaap": {}}}
    assert call_count["n"] == 3, \
        f"expected 3 urlopen calls (2 retries + 1 success), got {call_count['n']}"


def test_http_get_json_propagates_404() -> None:
    """HTTP 404 must surface as urllib.error.HTTPError on the FIRST call
    (no retry, not wrapped). fetch_one's ``no XBRL facts at SEC`` branch at
    edgar_fetcher.py line ~277 depends on this contract — if 404 got retried
    or wrapped, every CIK without companyfacts would stall for 5 attempts
    and then surface as HttpRetryExhausted instead of ``no_facts``."""
    call_count = {"n": 0}

    def side_effect(req, timeout):
        call_count["n"] += 1
        raise _http_error(404)

    raised: object = None
    with patch.object(edgar_fetcher, "SEC_BUCKET", _RecordingBucket()), \
            patch("http_client.urllib.request.urlopen", side_effect=side_effect), \
            patch("http_client.time.sleep", side_effect=_silence_sleep([])):
        try:
            edgar_fetcher.http_get_json("http://example.test/missing.json")
        except urllib.error.HTTPError as e:
            raised = e
        except http_client.HttpRetryExhausted as e:
            raised = ("wrapped", e)

    assert isinstance(raised, urllib.error.HTTPError), \
        f"expected urllib.error.HTTPError, got {type(raised).__name__}: {raised!r}"
    assert raised.code == 404, f"expected .code == 404, got {raised.code}"
    assert call_count["n"] == 1, \
        f"404 must not retry — got {call_count['n']} urlopen calls"


# --- runner ------------------------------------------------------------------

def run_all() -> int:
    failed = 0
    for test in (
        test_http_get_json_uses_token_bucket,
        test_http_get_json_retries_on_429,
        test_http_get_json_propagates_404,
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
    print("test_edgar_fetcher_http")
    rc = run_all()
    sys.exit(1 if rc else 0)
