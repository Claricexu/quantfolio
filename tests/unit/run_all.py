"""Run all backtest_engine unit tests, report pass/fail counts.

Plain-assert style, no pytest required. Each test file exposes a
``run_all()`` function returning an int failure count.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.unit import (
    test_config_hash,
    test_backtest_engine_edge_cases,
    test_backtest_engine_basic,
    test_api_backtest_wire_format,
    test_http_client,
    test_edgar_fetcher_http,
    test_yfinance_http,
    test_predict_ticker_warnings,
    test_classifier,
    test_peer_median,
    test_signal_alerts,
    test_alerts_send_manual,
    test_backrefresh_scheduler,
)


def main() -> int:
    total_fail = 0
    for name, mod in (
        ("test_config_hash", test_config_hash),
        ("test_backtest_engine_edge_cases", test_backtest_engine_edge_cases),
        ("test_backtest_engine_basic", test_backtest_engine_basic),
        ("test_api_backtest_wire_format", test_api_backtest_wire_format),
        ("test_http_client", test_http_client),
        ("test_edgar_fetcher_http", test_edgar_fetcher_http),
        ("test_yfinance_http", test_yfinance_http),
        ("test_predict_ticker_warnings", test_predict_ticker_warnings),
        ("test_classifier", test_classifier),
        ("test_peer_median", test_peer_median),
        ("test_signal_alerts", test_signal_alerts),
        ("test_alerts_send_manual", test_alerts_send_manual),
        ("test_backrefresh_scheduler", test_backrefresh_scheduler),
    ):
        print(f"\n=== {name} ===")
        fails = mod.run_all()
        total_fail += fails
        print(f"[{name}] failures={fails}")
    print(f"\n======== TOTAL FAILURES: {total_fail} ========")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
