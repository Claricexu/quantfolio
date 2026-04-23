"""Unit tests for finance_model_v2._download_batch's http_client wiring
(Phase 4.2 of C-5).

Pins the contract that:
    1. _download_batch routes through the module-level YF_BUCKET TokenBucket.
    2. Empty DataFrames (yfinance's silent-429 signal) are now retried —
       closing the C-5 silent-drop gap where the pre-refactor loop only
       retried on exceptions.
    3. After MAX_RETRIES exhausted, HttpRetryExhausted is raised (not the
       legacy RuntimeError).

Plain-assert style, no pytest. Runs via ``python tests/unit/run_all.py``.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import finance_model_v2
import http_client


# --- helpers -----------------------------------------------------------------

class _RecordingBucket:
    """TokenBucket stand-in that records every ``acquire()`` call.

    Looks like a TokenBucket to retrying_df_fetch (only .acquire() is used)
    but doesn't actually sleep or rate-limit — lets us assert the wiring
    without slowing the test down."""

    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


def _silence_sleep(sleeps: list[float]):
    """Return a patched time.sleep that records the delay and returns immediately."""

    def fake_sleep(s: float) -> None:
        sleeps.append(float(s))

    return fake_sleep


# --- tests -------------------------------------------------------------------

def test_download_batch_uses_yf_bucket() -> None:
    """_download_batch must route through YF_BUCKET so every yfinance batch
    call is paced by the shared token bucket (closes the ``no rate limiter on
    the yfinance path`` gap flagged by C-5)."""
    good_df = pd.DataFrame({'Close': [1.0, 2.0, 3.0]})

    recording = _RecordingBucket()
    with patch.object(finance_model_v2, "YF_BUCKET", recording), \
            patch.object(finance_model_v2.yf, "download", return_value=good_df) as mock_dl:
        result = finance_model_v2._download_batch(['FAKE'], '2020-01-01')

    assert result is good_df, "expected the non-empty DataFrame to be returned as-is"
    assert recording.calls == 1, \
        f"YF_BUCKET.acquire() should be called exactly once per batch, got {recording.calls}"
    assert mock_dl.call_count == 1, \
        f"yf.download should be called exactly once on success, got {mock_dl.call_count}"


def test_download_batch_retries_on_empty_then_succeeds() -> None:
    """Two empty DataFrames followed by a non-empty one should retry and
    succeed. This pins the C-5 fix: pre-refactor, yf.download returning an
    empty frame was treated as success and silently dropped the whole batch
    downstream. Now retrying_df_fetch treats empty-on-return as retryable."""
    empty_df = pd.DataFrame()
    good_df = pd.DataFrame({'Close': [1.0, 2.0, 3.0]})

    responses = [empty_df, empty_df, good_df]

    def side_effect(*args, **kwargs):
        return responses.pop(0)

    sleeps: list[float] = []
    with patch.object(finance_model_v2, "YF_BUCKET", _RecordingBucket()), \
            patch.object(finance_model_v2.yf, "download", side_effect=side_effect) as mock_dl, \
            patch("http_client.time.sleep", side_effect=_silence_sleep(sleeps)):
        result = finance_model_v2._download_batch(['FAKE'], '2020-01-01')

    assert result is good_df, "expected the third (non-empty) DataFrame to be returned"
    assert mock_dl.call_count == 3, \
        f"expected 3 yf.download calls (2 empty retries + 1 success), got {mock_dl.call_count}"


def test_download_batch_raises_after_exhaustion() -> None:
    """When yf.download always returns empty, retrying_df_fetch must exhaust
    its retry budget and raise HttpRetryExhausted — NOT the legacy
    RuntimeError the pre-refactor loop raised. MAX_RETRIES=5 means exactly
    5 yf.download attempts (the loop is range(1, max_retries+1))."""
    empty_df = pd.DataFrame()

    sleeps: list[float] = []
    raised: object = None
    with patch.object(finance_model_v2, "YF_BUCKET", _RecordingBucket()), \
            patch.object(finance_model_v2.yf, "download", return_value=empty_df) as mock_dl, \
            patch("http_client.time.sleep", side_effect=_silence_sleep(sleeps)):
        try:
            finance_model_v2._download_batch(['FAKE'], '2020-01-01')
        except http_client.HttpRetryExhausted as e:
            raised = e
        except RuntimeError as e:  # legacy exception type — should NOT fire
            raised = ("legacy_runtime_error", e)

    assert isinstance(raised, http_client.HttpRetryExhausted), \
        f"expected HttpRetryExhausted, got {type(raised).__name__}: {raised!r}"
    assert mock_dl.call_count == finance_model_v2.MAX_RETRIES, \
        f"expected MAX_RETRIES ({finance_model_v2.MAX_RETRIES}) yf.download calls, got {mock_dl.call_count}"


# --- runner ------------------------------------------------------------------

def run_all() -> int:
    failed = 0
    for test in (
        test_download_batch_uses_yf_bucket,
        test_download_batch_retries_on_empty_then_succeeds,
        test_download_batch_raises_after_exhaustion,
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
    print("test_yfinance_http")
    rc = run_all()
    sys.exit(1 if rc else 0)
