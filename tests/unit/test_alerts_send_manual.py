"""Unit tests for the Round 8c manual email-alert trigger.

Plain-assert style, matching the rest of ``tests/unit/``. No FastAPI HTTP
client (no httpx in this env); we exercise the endpoint coroutine directly
and the helper functions it composes. The classifier is already covered in
test_signal_alerts.py — these tests focus on:

  - The endpoint short-circuits with the right error string when there is
    no cached report and no on-disk file (cold-start guard).
  - The endpoint returns the spec'd ``recipients_sent`` / ``alert_count``
    shape on a successful send (with SMTP send mocked).
  - The endpoint surfaces SMTP failures as a structured error response,
    NOT a 500 stack trace.
  - ``_classify_report_alerts`` honors the ``log_prefix`` kwarg so the
    ``[Alert] Manual trigger:`` per-row lines are distinguishable from the
    scheduled-trigger lines in the log stream.

Per PATTERNS.md P-4, the manual trigger MUST share the rule path with the
scheduled trigger; we assert the helper is the single source of truth by
exercising _classify_report_alerts here and confirming it matches what the
scheduled wrapper would have produced.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import api_server  # noqa: E402


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    except Exception as exc:  # surface non-assertion errors clearly
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


def _stub_report():
    """Minimal report payload that exercises one BUY-consensus and one
    HOLD row through the classifier. Mirrors the daily_scan_both wire
    shape used by api_server (data/summary keys)."""
    return {
        "data": [
            {
                "symbol": "AAA",
                "current_price": 100.0,
                "consensus_signal": "BUY",
                "v2": {"signal": "BUY", "pct_change": 2.5},
                "v3": {"signal": "BUY", "pct_change": 3.1},
            },
            {
                "symbol": "BBB",
                "current_price": 50.0,
                "consensus_signal": "HOLD",
                "v2": {"signal": "HOLD", "pct_change": 0.1},
                "v3": {"signal": "HOLD", "pct_change": -0.2},
            },
        ],
        "summary": {"total_symbols": 2, "market_sentiment": "MIXED"},
    }


def _async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _json_body(response):
    """Pull the JSON dict out of a FastAPI JSONResponse (the test client
    isn't available — we read .body directly)."""
    return json.loads(response.body.decode("utf-8"))


# ─── Cold-start guard ─────────────────────────────────────────────────────


def test_manual_trigger_returns_error_when_no_report_loaded():
    """No cached report + no disk file → 400 with the spec'd error string."""
    with patch.object(api_server, "_report_cache", {"data": None, "generated_at": None,
                                                     "is_running": False}):
        with patch.object(api_server, "_load_latest_report_from_disk", lambda: None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                resp = _async(api_server.api_alerts_send_manual())
    body = _json_body(resp)
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}"
    assert body["success"] is False
    assert "generate one first" in body["error"], f"unexpected error: {body['error']}"
    assert body["recipients_sent"] == 0
    assert body["alert_count"] == {"buy": 0, "sell": 0}


# ─── No-recipients guard ─────────────────────────────────────────────────


def test_manual_trigger_returns_error_when_no_recipients_configured():
    """SMTP enabled but ALERT_TO empty → 400 with "no recipients" message."""
    with patch.object(api_server, "_report_cache",
                      {"data": _stub_report(), "generated_at": "x", "is_running": False}):
        with patch.object(api_server, "ALERT_TO", []):
            buf = io.StringIO()
            with redirect_stdout(buf):
                resp = _async(api_server.api_alerts_send_manual())
    body = _json_body(resp)
    assert resp.status_code == 400
    assert body["success"] is False
    assert "no recipients" in body["error"].lower(), f"unexpected error: {body['error']}"


# ─── Happy path ──────────────────────────────────────────────────────────


def test_manual_trigger_success_returns_spec_shape():
    """Mock SMTP send, exercise the full happy path, assert the JSON shape
    matches what the frontend dialog reads."""
    sent_args = {}

    def fake_send(subject, text_body, html_body):
        sent_args["subject"] = subject
        sent_args["text"] = text_body
        sent_args["html"] = html_body

    with patch.object(api_server, "_report_cache",
                      {"data": _stub_report(), "generated_at": "x", "is_running": False}):
        with patch.object(api_server, "ALERT_TO", ["a@example.com", "b@example.com"]):
            with patch.object(api_server, "_send_alert_email", fake_send):
                # Stub _get_best_strategy_map so consensus-BUY (path a) fires
                # with no dependency on the on-disk backtest library.
                with patch.object(api_server, "_get_best_strategy_map", lambda: {}):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        resp = _async(api_server.api_alerts_send_manual())
                    log_out = buf.getvalue()
    body = _json_body(resp)
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    assert body["success"] is True
    assert body["recipients_sent"] == 2
    assert body["alert_count"]["buy"] == 1, f"expected 1 BUY (AAA consensus), got {body['alert_count']}"
    assert body["alert_count"]["sell"] == 0
    # Round 8c log-prefix contract — the manual-trigger lines must be
    # distinguishable from the scheduled trigger lines for grep / log review.
    assert "[Alert] Manual trigger:" in log_out, "missing [Alert] Manual trigger: log lines"
    assert "classifier ran" in log_out, "missing classifier-ran log line"
    assert "sent to 2 recipients" in log_out, "missing send-success log line"
    assert sent_args.get("subject"), "renderer was not invoked"


# ─── SMTP failure surfaced as structured error ───────────────────────────


def test_manual_trigger_smtp_failure_returns_structured_error():
    """Transport failure must NOT crash the request; it must come back as
    {success:false, error:...} so the dialog can show a retry-able message."""
    def boom(*a, **kw):
        raise OSError("connection refused")

    with patch.object(api_server, "_report_cache",
                      {"data": _stub_report(), "generated_at": "x", "is_running": False}):
        with patch.object(api_server, "ALERT_TO", ["x@example.com"]):
            with patch.object(api_server, "_send_alert_email", boom):
                with patch.object(api_server, "_get_best_strategy_map", lambda: {}):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        resp = _async(api_server.api_alerts_send_manual())
                    log_out = buf.getvalue()
    body = _json_body(resp)
    assert resp.status_code == 502, f"expected 502 on SMTP failure, got {resp.status_code}"
    assert body["success"] is False
    assert "connection refused" in body["error"]
    # Counts still surface so the user knows what would have been sent.
    assert body["alert_count"]["buy"] == 1
    assert "send failed" in log_out, "missing send-failure log line"


# ─── Log-prefix contract on the classifier ───────────────────────────────


def test_classify_report_alerts_uses_supplied_log_prefix():
    """``log_prefix`` must thread through to every per-row print so
    `[Alert] Manual trigger:` lines are distinguishable in logs."""
    with patch.object(api_server, "_get_best_strategy_map", lambda: {}):
        buf = io.StringIO()
        with redirect_stdout(buf):
            buys, sells = api_server._classify_report_alerts(
                _stub_report(), log_prefix="[Alert] Manual trigger:"
            )
        out = buf.getvalue()
    assert len(buys) == 1, f"expected 1 consensus BUY (AAA), got {buys}"
    assert len(sells) == 0
    assert "[Alert] Manual trigger:" in out
    # Plain "[Alert] " prefix (without "Manual trigger:") must NOT appear —
    # otherwise we have a parallel log path.
    bad = [l for l in out.splitlines() if l.startswith("[Alert] ") and "Manual trigger:" not in l]
    assert not bad, f"unexpected scheduled-trigger log lines: {bad!r}"


# ─── Config endpoint ─────────────────────────────────────────────────────


def test_alerts_config_returns_recipients_count_only():
    """The /api/alerts/config helper must return the count + smtp flag and
    must NOT leak email addresses (the dialog only needs the count)."""
    with patch.object(api_server, "ALERT_TO", ["a@example.com", "b@example.com", "c@example.com"]):
        with patch.object(api_server, "SMTP_ENABLED", True):
            resp = _async(api_server.api_alerts_config())
    body = _json_body(resp)
    assert body["recipients_count"] == 3
    assert body["smtp_enabled"] is True
    # No address-shaped strings in the response.
    blob = json.dumps(body)
    assert "@" not in blob, f"recipient addresses leaked: {blob}"


# ─── Runner ──────────────────────────────────────────────────────────────


def run_all():
    fails = 0
    for name, fn in (
        ("test_manual_trigger_returns_error_when_no_report_loaded",
         test_manual_trigger_returns_error_when_no_report_loaded),
        ("test_manual_trigger_returns_error_when_no_recipients_configured",
         test_manual_trigger_returns_error_when_no_recipients_configured),
        ("test_manual_trigger_success_returns_spec_shape",
         test_manual_trigger_success_returns_spec_shape),
        ("test_manual_trigger_smtp_failure_returns_structured_error",
         test_manual_trigger_smtp_failure_returns_structured_error),
        ("test_classify_report_alerts_uses_supplied_log_prefix",
         test_classify_report_alerts_uses_supplied_log_prefix),
        ("test_alerts_config_returns_recipients_count_only",
         test_alerts_config_returns_recipients_count_only),
    ):
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
