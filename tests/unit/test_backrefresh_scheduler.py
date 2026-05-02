"""Unit tests for Round 8d biweekly backtest auto-refresh.

Plain-assert style, matching the rest of ``tests/unit/``. Tests the small
helpers extracted from ``_run_backtest_batch`` / ``_biweekly_backtest_refresh_job``
so the FastAPI app does not need to boot:

  - _compute_parity_match    (ISO-week parity)
  - _classify_ticker_outcome (Case B vs failed)
  - _write_refresh_state_file (state-file writer)
  - _prune_orphan_cache       (Tickers.csv cleanup)
  - _biweekly_backtest_refresh_job (concurrency guard, via stdout capture)
  - majority-failure log line  (stdout capture inside _run_backtest_batch)
  - GET /api/backtest-refresh/status (file missing / present / corrupted)

Round 8d baseline: importing api_server at module level is safe because
_start_scheduler is only invoked inside the FastAPI lifespan context.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import api_server  # noqa: E402
from api_server import (  # noqa: E402
    _compute_parity_match,
    _classify_ticker_outcome,
    _write_refresh_state_file,
    _prune_orphan_cache,
    _biweekly_backtest_refresh_job,
    _run_backtest_batch,
    _INSUFFICIENT_DATA_ERR,
    BACKTEST_REFRESH_REFERENCE_WEEK,
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


# ─── ISO-week parity ───────────────────────────────────────────────────────

# 1. same week as reference → parity matches
def test_parity_same_week_matches():
    assert _compute_parity_match(19, 19) is True


# 2. one week after reference → off-week
def test_parity_one_week_after_off():
    assert _compute_parity_match(20, 19) is False


# 3. two weeks after reference → on-week
def test_parity_two_weeks_after_on():
    assert _compute_parity_match(21, 19) is True


# 4. year-boundary case: ref=51, current=1 → (1 - 51) % 2 == 0 in Python
def test_parity_year_boundary_works():
    # (1 - 51) = -50, -50 % 2 == 0 → parity matches
    assert _compute_parity_match(1, 51) is True
    # (2 - 51) = -49, -49 % 2 == 1 → off-week
    assert _compute_parity_match(2, 51) is False


# ─── Case B classification ────────────────────────────────────────────────

# 10. _bt_cache entry with the production insufficient-data string → 'case_b'
def test_classify_insufficient_data_routes_to_case_b():
    entry = {'status': 'error', 'error': _INSUFFICIENT_DATA_ERR}
    assert _classify_ticker_outcome(entry) == 'case_b'


# 11. generic exception entry → 'failed'
def test_classify_generic_error_routes_to_failed():
    entry = {'status': 'error', 'error': 'KeyError: missing column foo'}
    assert _classify_ticker_outcome(entry) == 'failed'


def test_classify_done_status_returns_completed():
    entry = {'status': 'done', 'data': {'symbol': 'AAPL'}}
    assert _classify_ticker_outcome(entry) == 'completed'


def test_classify_missing_entry_returns_failed():
    assert _classify_ticker_outcome({}) == 'failed'
    assert _classify_ticker_outcome(None) == 'failed'


# ─── State-file writer ────────────────────────────────────────────────────

# 5. file shape includes all required keys
def test_state_file_write_has_correct_shape():
    state = {
        "trigger": "scheduled",
        "completed_at": "2026-05-08T21:34:12-04:00",
        "started_at": 1715216000.0,
        "total": 12,
        "completed": 12,
        "skipped_cached": 150,
        "completed_symbols": ["AAPL", "MSFT"],
        "failed_symbols": ["BADTICK"],
        "case_b_skipped": ["TEM"],
        "error": None,
    }
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "last_backtest_refresh.json")
        _write_refresh_state_file(state, path)
        with open(path) as f:
            loaded = json.load(f)
        for key in ("trigger", "completed_at", "started_at", "total",
                    "completed", "skipped_cached", "completed_symbols",
                    "failed_symbols", "case_b_skipped", "error"):
            assert key in loaded, f"missing key {key!r} in state file"
        assert loaded["trigger"] == "scheduled"
        assert loaded["completed_symbols"] == ["AAPL", "MSFT"]
        assert loaded["case_b_skipped"] == ["TEM"]


# ─── Status endpoint ──────────────────────────────────────────────────────

def _call_refresh_status_endpoint():
    """Invoke the FastAPI handler directly (no httpx TestClient dep needed).
    Returns (status_code, body_dict). The handler is async, so use asyncio.run."""
    import asyncio
    res = asyncio.run(api_server.api_backtest_refresh_status())
    body = json.loads(res.body.decode("utf-8"))
    return res.status_code, body


# 6. file missing → 404 with no_run_yet
def test_status_endpoint_missing_returns_404():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "nonexistent.json")
        with patch.object(api_server, "BACKTEST_REFRESH_STATE_FILE", path):
            status, body = _call_refresh_status_endpoint()
            assert status == 404, f"expected 404, got {status}"
            assert body == {"status": "no_run_yet"}


# 7. file present → 200 with content
def test_status_endpoint_present_returns_200():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "last_backtest_refresh.json")
        payload = {"trigger": "manual", "completed": 5, "case_b_skipped": ["TEM"]}
        with open(path, "w") as f:
            json.dump(payload, f)
        with patch.object(api_server, "BACKTEST_REFRESH_STATE_FILE", path):
            status, body = _call_refresh_status_endpoint()
            assert status == 200, f"expected 200, got {status}"
            assert body["trigger"] == "manual"
            assert body["case_b_skipped"] == ["TEM"]


# 8. corrupted JSON → 500
def test_status_endpoint_corrupted_returns_500():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "last_backtest_refresh.json")
        with open(path, "w") as f:
            f.write("{not valid json")
        with patch.object(api_server, "BACKTEST_REFRESH_STATE_FILE", path):
            status, body = _call_refresh_status_endpoint()
            assert status == 500, f"expected 500, got {status}"
            assert "error" in body


# ─── Concurrency guard ────────────────────────────────────────────────────

# 9. when is_running=True, scheduler wrapper logs skip and does NOT spawn thread
def test_scheduler_skips_when_manual_already_running():
    """Force parity match (so the parity guard does not short-circuit) and
    set is_running=True. Wrapper must log "manual batch already in progress"
    and NOT spawn a worker thread."""
    saved_running = api_server._batch_state["is_running"]
    api_server._batch_state["is_running"] = True

    spawned = {"count": 0}
    real_thread_init = threading.Thread.__init__

    def _spy_init(self, *args, **kwargs):
        spawned["count"] += 1
        real_thread_init(self, *args, **kwargs)

    # Patch _compute_parity_match to always return True so we hit the lock
    # check (the path under test). This isolates the concurrency guard from
    # the parity guard.
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), \
             patch.object(api_server, "_compute_parity_match", return_value=True), \
             patch.object(threading.Thread, "__init__", _spy_init):
            _biweekly_backtest_refresh_job()
    finally:
        api_server._batch_state["is_running"] = saved_running

    output = buf.getvalue()
    assert "Skipping scheduled run" in output, f"expected skip log, got: {output!r}"
    assert "manual batch already in progress" in output, f"unexpected skip reason: {output!r}"
    assert spawned["count"] == 0, "no thread should have been spawned"


# ─── Majority-failure detection (via stdout capture in _run_backtest_batch) ──

# 12a. 3 failed + 1 completed → CRITICAL line
def test_majority_failure_logs_critical():
    """Synthesize state directly and exercise just the post-sweep log path.
    We do not run real backtests — we patch _run_backtest_chart to be a no-op
    and seed _bt_cache so _run_backtest_batch routes to specific buckets.
    """
    saved_state = dict(api_server._batch_state)
    saved_lock = api_server._batch_lock  # same lock, not replaced
    saved_bt = dict(api_server._bt_cache)

    try:
        # Use 4 fake symbols. 3 fail with generic error, 1 succeeds.
        fake_syms = ["AAA", "BBB", "CCC", "DDD"]

        # Pre-populate _bt_cache so _run_backtest_chart's "no-op" leaves the
        # outcome we want for each ticker.
        api_server._bt_cache.clear()
        api_server._bt_cache["AAA"] = {'status': 'done', 'data': {}}
        api_server._bt_cache["BBB"] = {'status': 'error', 'error': 'boom'}
        api_server._bt_cache["CCC"] = {'status': 'error', 'error': 'kaboom'}
        api_server._bt_cache["DDD"] = {'status': 'error', 'error': 'splat'}

        api_server._batch_state.update({
            "is_running": True, "total": 4, "completed": 0,
            "current_symbol": None, "completed_symbols": [],
            "failed_symbols": [], "case_b_skipped": [],
            "skipped_cached": 0, "started_at": 0, "error": None,
        })

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, "last_backtest_refresh.json")
            cache_path = td  # use temp dir as cache dir for prune step
            with redirect_stdout(buf), \
                 patch.object(api_server, "get_all_symbols", return_value=fake_syms), \
                 patch.object(api_server, "_run_backtest_chart", lambda s: None), \
                 patch.object(api_server, "BACKTEST_REFRESH_STATE_FILE", state_path), \
                 patch.object(api_server, "CACHE_DIR", cache_path):
                _run_backtest_batch(trigger="manual")

        output = buf.getvalue()
        assert "CRITICAL: majority failure" in output, f"missing CRITICAL line:\n{output}"
        assert "(3/4)" in output, f"wrong M/N ratio:\n{output}"
    finally:
        api_server._bt_cache.clear()
        api_server._bt_cache.update(saved_bt)
        api_server._batch_state.clear()
        api_server._batch_state.update(saved_state)


# 12b. 1 failed + 3 completed → no CRITICAL line
def test_minority_failure_no_critical_log():
    saved_state = dict(api_server._batch_state)
    saved_bt = dict(api_server._bt_cache)
    try:
        fake_syms = ["AAA", "BBB", "CCC", "DDD"]
        api_server._bt_cache.clear()
        api_server._bt_cache["AAA"] = {'status': 'done', 'data': {}}
        api_server._bt_cache["BBB"] = {'status': 'done', 'data': {}}
        api_server._bt_cache["CCC"] = {'status': 'done', 'data': {}}
        api_server._bt_cache["DDD"] = {'status': 'error', 'error': 'boom'}

        api_server._batch_state.update({
            "is_running": True, "total": 4, "completed": 0,
            "current_symbol": None, "completed_symbols": [],
            "failed_symbols": [], "case_b_skipped": [],
            "skipped_cached": 0, "started_at": 0, "error": None,
        })

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, "last_backtest_refresh.json")
            cache_path = td
            with redirect_stdout(buf), \
                 patch.object(api_server, "get_all_symbols", return_value=fake_syms), \
                 patch.object(api_server, "_run_backtest_chart", lambda s: None), \
                 patch.object(api_server, "BACKTEST_REFRESH_STATE_FILE", state_path), \
                 patch.object(api_server, "CACHE_DIR", cache_path):
                _run_backtest_batch(trigger="manual")

        output = buf.getvalue()
        assert "CRITICAL" not in output, f"unexpected CRITICAL line:\n{output}"
    finally:
        api_server._bt_cache.clear()
        api_server._bt_cache.update(saved_bt)
        api_server._batch_state.clear()
        api_server._batch_state.update(saved_state)


# ─── Orphan cleanup ───────────────────────────────────────────────────────

# 13. only-AAPL valid set leaves AAPL file but prunes XXX file
def test_prune_orphan_cache_removes_unknown_tickers():
    with tempfile.TemporaryDirectory() as td:
        # Create three cache files: AAPL (valid), XXX (orphan), and an
        # unrelated file (should NOT be touched — wrong filename pattern).
        for fname in ("backtest_chart_AAPL.json", "backtest_chart_XXX.json", "other_file.json"):
            with open(os.path.join(td, fname), "w") as f:
                f.write("{}")
        valid = {"AAPL"}
        pruned = _prune_orphan_cache(td, valid)
        remaining = sorted(os.listdir(td))
        assert "backtest_chart_AAPL.json" in remaining
        assert "backtest_chart_XXX.json" not in remaining
        assert "other_file.json" in remaining, "unrelated files must not be pruned"
        assert pruned == ["backtest_chart_XXX.json"]


def test_prune_orphan_cache_handles_empty_dir():
    with tempfile.TemporaryDirectory() as td:
        pruned = _prune_orphan_cache(td, {"AAPL"})
        assert pruned == []


# ─── Runner ────────────────────────────────────────────────────────────────


def run_all():
    fails = 0
    for name, fn in (
        ("test_parity_same_week_matches", test_parity_same_week_matches),
        ("test_parity_one_week_after_off", test_parity_one_week_after_off),
        ("test_parity_two_weeks_after_on", test_parity_two_weeks_after_on),
        ("test_parity_year_boundary_works", test_parity_year_boundary_works),
        ("test_classify_insufficient_data_routes_to_case_b",
         test_classify_insufficient_data_routes_to_case_b),
        ("test_classify_generic_error_routes_to_failed",
         test_classify_generic_error_routes_to_failed),
        ("test_classify_done_status_returns_completed",
         test_classify_done_status_returns_completed),
        ("test_classify_missing_entry_returns_failed",
         test_classify_missing_entry_returns_failed),
        ("test_state_file_write_has_correct_shape",
         test_state_file_write_has_correct_shape),
        ("test_status_endpoint_missing_returns_404",
         test_status_endpoint_missing_returns_404),
        ("test_status_endpoint_present_returns_200",
         test_status_endpoint_present_returns_200),
        ("test_status_endpoint_corrupted_returns_500",
         test_status_endpoint_corrupted_returns_500),
        ("test_scheduler_skips_when_manual_already_running",
         test_scheduler_skips_when_manual_already_running),
        ("test_majority_failure_logs_critical",
         test_majority_failure_logs_critical),
        ("test_minority_failure_no_critical_log",
         test_minority_failure_no_critical_log),
        ("test_prune_orphan_cache_removes_unknown_tickers",
         test_prune_orphan_cache_removes_unknown_tickers),
        ("test_prune_orphan_cache_handles_empty_dir",
         test_prune_orphan_cache_handles_empty_dir),
    ):
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
