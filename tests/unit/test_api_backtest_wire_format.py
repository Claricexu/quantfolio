"""Wire-format guard for backtest_multi_strategy (Wright note #3, Phase 4b).

backtest_multi_strategy is the function that feeds ``/api/backtest-chart``
(via ``_run_backtest_chart`` in api_server.py). Its return shape is a public
contract because the frontend consumes it keyed-by-name via
``_run_backtest_chart``'s assembly of ``result['strategies']``.

After the Phase 4b refactor, the function routes through
``BacktestEngine.run_multi``. ``BacktestResult`` exposes many extra fields
(``raw_predictions``, ``z_scores``, ``config_hash``, ``sortino``, ``trades``,
``per_period_returns``, ``versions``, ``ensemble_builder``, ``min_zscore_samples_used``,
``min_train_days_used``, ``retrain_freq_days``, ``feature_version``,
``num_trades``, ``final_portfolio_value``, ``start_date``, ``end_date``, …)
which the wrapper must NOT leak into the wire response — the frontend has
never seen them, and silently adding them would break the disk cache schema.

This test asserts the wrapper's returned dict has EXACTLY the legacy key set:

    top-level: {symbol, version, version_label, period_days, start_date, dates,
                buyhold, full, buy_only, buyhold_stats}
    full / buy_only sub-dict: {portfolio, return_pct, sharpe, max_drawdown,
                               buys, sells}
    buyhold_stats: {return_pct, sharpe, max_drawdown, buys, sells}

If a future refactor passes a BacktestResult through raw, this test fails
before the change reaches api_server.py.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import finance_model_v2 as fm  # noqa: E402
from finance_model_v2 import backtest_multi_strategy  # noqa: E402


EXPECTED_TOP_LEVEL = {
    "symbol", "version", "version_label", "period_days", "start_date",
    "dates", "buyhold", "full", "buy_only", "buyhold_stats",
}

EXPECTED_SUB = {"portfolio", "return_pct", "sharpe", "max_drawdown", "buys", "sells"}

EXPECTED_BUYHOLD_STATS = {"return_pct", "sharpe", "max_drawdown", "buys", "sells"}

# Engine-only keys that must NEVER leak to the wire.
FORBIDDEN_LEAKS = {
    "raw_predictions", "z_scores", "config_hash", "sortino", "trades",
    "per_period_returns", "versions", "ensemble_builder",
    "min_zscore_samples_used", "min_train_days_used", "retrain_freq_days",
    "feature_version", "num_trades", "final_portfolio_value",
    "end_date", "initial_cash",
}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_wire_format_backtest_multi_strategy_top_level_keys() -> None:
    """Top-level dict keys must match the legacy schema exactly."""
    result = backtest_multi_strategy(
        "KO",
        cache_dir=os.path.join(REPO_ROOT, "data_cache"),
        version="v3",
    )
    _assert(result is not None, "backtest_multi_strategy returned None for KO")
    keys = set(result.keys())
    missing = EXPECTED_TOP_LEVEL - keys
    extra = keys - EXPECTED_TOP_LEVEL
    _assert(not missing, f"missing top-level keys: {sorted(missing)}")
    _assert(not extra, f"unexpected top-level keys leaked: {sorted(extra)}")
    # Belt-and-suspenders: specifically check no engine-only key slipped in.
    leaked = keys & FORBIDDEN_LEAKS
    _assert(not leaked, f"engine-only key(s) leaked to wire: {sorted(leaked)}")
    print(f"  [wire] top-level keys OK: {sorted(keys)}")


def test_wire_format_full_subdict_keys() -> None:
    """'full' and 'buy_only' sub-dicts must match the legacy schema."""
    result = backtest_multi_strategy(
        "KO",
        cache_dir=os.path.join(REPO_ROOT, "data_cache"),
        version="v3",
    )
    _assert(result is not None, "backtest_multi_strategy returned None for KO")

    for name in ("full", "buy_only"):
        sub = result[name]
        _assert(isinstance(sub, dict), f"{name} is not a dict: {type(sub).__name__}")
        skeys = set(sub.keys())
        missing = EXPECTED_SUB - skeys
        extra = skeys - EXPECTED_SUB
        _assert(not missing, f"{name} missing keys: {sorted(missing)}")
        _assert(not extra, f"{name} unexpected keys leaked: {sorted(extra)}")
        leaked = skeys & FORBIDDEN_LEAKS
        _assert(not leaked, f"{name} leaked engine-only key(s): {sorted(leaked)}")

        # Type/shape sanity: portfolio is a list of floats; stats are scalars.
        _assert(isinstance(sub["portfolio"], list), f"{name}.portfolio not a list")
        _assert(len(sub["portfolio"]) == result["period_days"],
                f"{name}.portfolio length {len(sub['portfolio'])} != period_days {result['period_days']}")
        for k in ("return_pct", "sharpe", "max_drawdown"):
            _assert(isinstance(sub[k], (int, float)),
                    f"{name}.{k} not numeric: {type(sub[k]).__name__}")
        for k in ("buys", "sells"):
            _assert(isinstance(sub[k], int), f"{name}.{k} not int: {type(sub[k]).__name__}")
        print(f"  [wire] {name} sub-dict OK")


def test_wire_format_buyhold_stats_keys() -> None:
    """'buyhold_stats' sub-dict must match the legacy stats schema."""
    result = backtest_multi_strategy(
        "KO",
        cache_dir=os.path.join(REPO_ROOT, "data_cache"),
        version="v3",
    )
    _assert(result is not None, "backtest_multi_strategy returned None for KO")
    bhs = result["buyhold_stats"]
    _assert(isinstance(bhs, dict), f"buyhold_stats not a dict: {type(bhs).__name__}")
    skeys = set(bhs.keys())
    missing = EXPECTED_BUYHOLD_STATS - skeys
    extra = skeys - EXPECTED_BUYHOLD_STATS
    _assert(not missing, f"buyhold_stats missing keys: {sorted(missing)}")
    _assert(not extra, f"buyhold_stats unexpected keys: {sorted(extra)}")
    print(f"  [wire] buyhold_stats OK: {sorted(skeys)}")


def test_wire_format_dates_and_buyhold_shapes() -> None:
    """dates is a list[str]; buyhold is a list[float]; lengths match period_days."""
    result = backtest_multi_strategy(
        "KO",
        cache_dir=os.path.join(REPO_ROOT, "data_cache"),
        version="v3",
    )
    _assert(result is not None, "backtest_multi_strategy returned None for KO")
    _assert(isinstance(result["dates"], list), "dates not a list")
    _assert(len(result["dates"]) == result["period_days"],
            f"len(dates)={len(result['dates'])} != period_days={result['period_days']}")
    if result["dates"]:
        _assert(isinstance(result["dates"][0], str), "dates[0] not a string")
    _assert(isinstance(result["buyhold"], list), "buyhold not a list")
    _assert(len(result["buyhold"]) == result["period_days"],
            f"len(buyhold)={len(result['buyhold'])} != period_days={result['period_days']}")
    _assert(isinstance(result["version"], str), "version not a string")
    _assert(isinstance(result["version_label"], str), "version_label not a string")
    print(f"  [wire] dates+buyhold shapes OK ({result['period_days']} days)")


def run_all() -> int:
    tests = [
        test_wire_format_backtest_multi_strategy_top_level_keys,
        test_wire_format_full_subdict_keys,
        test_wire_format_buyhold_stats_keys,
        test_wire_format_dates_and_buyhold_shapes,
    ]
    fails = 0
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"  PASS  {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            fails += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            fails += 1
    return fails


if __name__ == "__main__":
    fails = run_all()
    sys.exit(0 if fails == 0 else 1)
