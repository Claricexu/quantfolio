"""Unit tests for Round 8d commit 2: Case B 8-week graduation cadence.

Plain-assert style, matching the rest of ``tests/unit/``. Tests:

  - _should_retry_case_b boundary behavior (1w / 8w / 9w)
  - Schema migration: commit-1 state file (no case_b_history) loads cleanly
  - Graduation flow: ticker in case_b_history for 9 weeks, then succeeds
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import api_server  # noqa: E402
from api_server import (  # noqa: E402
    _should_retry_case_b,
    _load_refresh_state,
    _run_backtest_batch,
)


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    except Exception as exc:
        print(f"  FAIL  {name}: unexpected {type(exc).__name__}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


# ─── _should_retry_case_b boundaries ──────────────────────────────────────

# 1. last skip 1 week ago → False
def test_retry_one_week_too_soon():
    now = datetime(2026, 5, 8, 21, 0, tzinfo=timezone.utc)
    last = now - timedelta(weeks=1)
    assert _should_retry_case_b(last.isoformat(), now) is False


# 2. last skip 9 weeks ago → True
def test_retry_nine_weeks_due():
    now = datetime(2026, 5, 8, 21, 0, tzinfo=timezone.utc)
    last = now - timedelta(weeks=9)
    assert _should_retry_case_b(last.isoformat(), now) is True


# 3. last skip exactly 8 weeks → True (boundary inclusive)
def test_retry_eight_weeks_boundary_inclusive():
    now = datetime(2026, 5, 8, 21, 0, tzinfo=timezone.utc)
    last = now - timedelta(weeks=8)
    assert _should_retry_case_b(last.isoformat(), now) is True


# Defensive: bad timestamp → True (never strand a ticker on parse error)
def test_retry_bad_timestamp_defaults_to_true():
    now = datetime(2026, 5, 8, 21, 0, tzinfo=timezone.utc)
    assert _should_retry_case_b("not-a-date", now) is True
    assert _should_retry_case_b("", now) is True
    assert _should_retry_case_b(None, now) is True


# ─── Schema migration ─────────────────────────────────────────────────────

# 4. commit-1 state file (no case_b_history key) loads cleanly
def test_schema_migration_initializes_case_b_history():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "last_backtest_refresh.json")
        legacy = {
            "trigger": "manual",
            "completed_at": "2026-04-25T21:00:00-04:00",
            "completed_symbols": ["AAPL"],
            "failed_symbols": [],
            "case_b_skipped": ["TEM"],
        }
        with open(path, "w") as f:
            json.dump(legacy, f)
        buf = io.StringIO()
        with redirect_stdout(buf):
            data = _load_refresh_state(path)
        assert "case_b_history" in data
        assert data["case_b_history"] == {}
        assert "Migrating state file" in buf.getvalue()
        # Legacy fields are preserved.
        assert data["completed_symbols"] == ["AAPL"]
        assert data["case_b_skipped"] == ["TEM"]


def test_load_refresh_state_missing_file_returns_empty_history():
    with tempfile.TemporaryDirectory() as td:
        data = _load_refresh_state(os.path.join(td, "does_not_exist.json"))
        assert data == {"case_b_history": {}}


def test_load_refresh_state_corrupted_returns_empty_history():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "broken.json")
        with open(path, "w") as f:
            f.write("{not valid json")
        data = _load_refresh_state(path)
        assert data == {"case_b_history": {}}


# ─── Graduation flow (synthesized end-to-end run) ─────────────────────────

# 5. ticker XYZ in case_b for 9 weeks → due for retry; mocked success
#    moves it to completed_symbols and removes it from case_b_history.
def test_graduation_flow_ticker_moves_from_case_b_to_completed():
    saved_state = dict(api_server._batch_state)
    saved_bt = dict(api_server._bt_cache)
    try:
        # Seed prior state file with XYZ in case_b_history 9 weeks ago.
        nine_weeks_ago = (
            datetime.now(timezone.utc) - timedelta(weeks=9)
        ).isoformat()
        prior = {
            "trigger": "scheduled",
            "completed_at": nine_weeks_ago,
            "completed_symbols": [],
            "failed_symbols": [],
            "case_b_skipped": ["XYZ"],
            "case_b_history": {"XYZ": nine_weeks_ago},
        }

        # Mock _run_backtest_chart to write a 'done' entry into _bt_cache.
        def _fake_run(sym):
            api_server._bt_cache[sym] = {'status': 'done', 'data': {'symbol': sym}}

        api_server._bt_cache.clear()
        api_server._batch_state.update({
            "is_running": True, "total": 0, "completed": 0,
            "current_symbol": None, "completed_symbols": [],
            "failed_symbols": [], "case_b_skipped": [],
            "skipped_cached": 0, "started_at": 0, "error": None,
        })

        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, "last_backtest_refresh.json")
            cache_path = td
            with open(state_path, "w") as f:
                json.dump(prior, f)

            buf = io.StringIO()
            with redirect_stdout(buf), \
                 patch.object(api_server, "get_all_symbols", return_value=["XYZ"]), \
                 patch.object(api_server, "_run_backtest_chart", _fake_run), \
                 patch.object(api_server, "BACKTEST_REFRESH_STATE_FILE", state_path), \
                 patch.object(api_server, "CACHE_DIR", cache_path):
                _run_backtest_batch(trigger="scheduled")

            output = buf.getvalue()
            # Verify graduation log line.
            assert "graduated from case B" in output, f"missing graduation log:\n{output}"

            # Verify state file was rewritten and case_b_history is now empty.
            with open(state_path) as f:
                final = json.load(f)
            assert "XYZ" in final["completed_symbols"], \
                f"XYZ should be in completed_symbols, got {final['completed_symbols']}"
            assert "XYZ" not in final.get("case_b_history", {}), \
                f"XYZ should be removed from case_b_history, got {final.get('case_b_history')}"
    finally:
        api_server._bt_cache.clear()
        api_server._bt_cache.update(saved_bt)
        api_server._batch_state.clear()
        api_server._batch_state.update(saved_state)


# 5b. complement: ticker in case_b for 1 week → retry-deferred (NOT run).
def test_retry_deferred_ticker_not_invoked():
    saved_state = dict(api_server._batch_state)
    saved_bt = dict(api_server._bt_cache)
    try:
        one_week_ago = (
            datetime.now(timezone.utc) - timedelta(weeks=1)
        ).isoformat()
        prior = {
            "trigger": "scheduled",
            "case_b_skipped": ["TEM"],
            "case_b_history": {"TEM": one_week_ago},
        }

        invocations = {"count": 0}

        def _fake_run(sym):
            invocations["count"] += 1
            api_server._bt_cache[sym] = {'status': 'done', 'data': {'symbol': sym}}

        api_server._bt_cache.clear()
        api_server._batch_state.update({
            "is_running": True, "total": 0, "completed": 0,
            "current_symbol": None, "completed_symbols": [],
            "failed_symbols": [], "case_b_skipped": [],
            "skipped_cached": 0, "started_at": 0, "error": None,
        })

        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, "last_backtest_refresh.json")
            cache_path = td
            with open(state_path, "w") as f:
                json.dump(prior, f)

            buf = io.StringIO()
            with redirect_stdout(buf), \
                 patch.object(api_server, "get_all_symbols", return_value=["TEM"]), \
                 patch.object(api_server, "_run_backtest_chart", _fake_run), \
                 patch.object(api_server, "BACKTEST_REFRESH_STATE_FILE", state_path), \
                 patch.object(api_server, "CACHE_DIR", cache_path):
                _run_backtest_batch(trigger="scheduled")

            assert invocations["count"] == 0, \
                "TEM should not have been invoked (still within 8-week window)"
            with open(state_path) as f:
                final = json.load(f)
            assert "TEM" in final["case_b_skipped"], \
                f"TEM should still appear in case_b_skipped, got {final['case_b_skipped']}"
            assert "TEM" in final.get("case_b_history", {}), \
                "case_b_history timestamp should be preserved (not overwritten)"
            assert final["case_b_history"]["TEM"] == one_week_ago, \
                "case_b_history timestamp should NOT be refreshed when retry-deferred"
    finally:
        api_server._bt_cache.clear()
        api_server._bt_cache.update(saved_bt)
        api_server._batch_state.clear()
        api_server._batch_state.update(saved_state)


# ─── Runner ────────────────────────────────────────────────────────────────


def run_all():
    fails = 0
    for name, fn in (
        ("test_retry_one_week_too_soon", test_retry_one_week_too_soon),
        ("test_retry_nine_weeks_due", test_retry_nine_weeks_due),
        ("test_retry_eight_weeks_boundary_inclusive",
         test_retry_eight_weeks_boundary_inclusive),
        ("test_retry_bad_timestamp_defaults_to_true",
         test_retry_bad_timestamp_defaults_to_true),
        ("test_schema_migration_initializes_case_b_history",
         test_schema_migration_initializes_case_b_history),
        ("test_load_refresh_state_missing_file_returns_empty_history",
         test_load_refresh_state_missing_file_returns_empty_history),
        ("test_load_refresh_state_corrupted_returns_empty_history",
         test_load_refresh_state_corrupted_returns_empty_history),
        ("test_graduation_flow_ticker_moves_from_case_b_to_completed",
         test_graduation_flow_ticker_moves_from_case_b_to_completed),
        ("test_retry_deferred_ticker_not_invoked",
         test_retry_deferred_ticker_not_invoked),
    ):
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
