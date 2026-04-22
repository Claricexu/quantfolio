"""Happy-path test for BacktestEngine.

Uses a small slice of real KO cache data so the fitted ensemble is realistic
(engineer_features_v3 requires enough history for 126-day SMA etc). Keeps the
test window small by capping the data at ~400 rows, so 1-2 retrains happen
and the test completes in a few seconds.

Assertions:
    * engine.run() returns a BacktestResult with the expected metadata shape
    * portfolio_curve has sensible length (> 0, matches signals/dates)
    * trade count == len(BUYs) + len(SELLs)
    * every fitted base estimator has n_jobs==1 (Wright note #5)
    * run_multi with {'full', 'buy_only'} produces two results that share the
      same raw_predictions list (byte-identical)
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import numpy as np
import pandas as pd

import finance_model_v2 as fm
from backtest_engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    buy_only,
    full_signal,
)


def _load_small_ko_frame() -> pd.DataFrame:
    """Load a capped-length KO frame, post-engineered and post-dropna."""
    raw = fm.fetch_stock_data(["KO"], cache_dir=os.path.join(REPO_ROOT, "data_cache"))
    df = raw["KO"].copy()
    df = fm.engineer_features_v3(df)
    dc = df.dropna(subset=["Target_Return"] + fm.V3_FEATURE_COLS).copy()
    # Cap at 400 rows to keep the test fast. The engine's walk-forward skips
    # the first min_train_days rows anyway, so we still get ~50 simulation
    # steps with ~1 retrain at retrain_freq_days=63.
    dc = dc.tail(400)
    return dc


def test_run_single_strategy() -> None:
    dc = _load_small_ko_frame()
    cfg = BacktestConfig(
        symbol="KO",
        strategy_name="full",
        retrain_freq_days=63,
        min_train_days=300,  # force quick start
        ensemble_builder="oof",
        feature_version="v3",
    )
    engine = BacktestEngine(cfg, dc)
    result = engine.run(full_signal)

    assert isinstance(result, BacktestResult), f"expected BacktestResult, got {type(result)}"
    assert result.symbol == "KO"
    assert result.strategy == "full"
    assert result.ensemble_builder == "oof"
    assert result.feature_version == "v3"
    assert result.retrain_freq_days == 63
    assert result.min_zscore_samples_used == 20
    assert result.min_train_days_used == 300
    assert len(result.portfolio_curve) > 0
    assert len(result.portfolio_curve) == len(result.signals)
    assert len(result.portfolio_curve) == len(result.dates)
    assert len(result.raw_predictions) == len(result.portfolio_curve)
    assert len(result.z_scores) == len(result.portfolio_curve)
    # Per-period returns has length == portfolio - 1
    assert len(result.per_period_returns) == len(result.portfolio_curve) - 1
    # Trade count consistent
    assert result.num_trades == result.buys + result.sells
    assert len(result.trades) == result.num_trades
    # Holds + buys + sells = period_days
    assert result.buys + result.sells + result.holds == result.period_days
    # Final portfolio is positive
    assert result.final_portfolio_value > 0
    # config_hash is a 64-char hex string
    assert len(result.config_hash) == 64
    assert all(c in "0123456789abcdef" for c in result.config_hash)
    print(f"  [basic] single-strategy run OK: {result.period_days}d, "
          f"final=${result.final_portfolio_value:.2f}, "
          f"trades={result.num_trades}, sharpe={result.sharpe:.2f}")


def test_run_multi_shares_predictions() -> None:
    """run_multi must produce byte-identical raw_predictions across strategies."""
    dc = _load_small_ko_frame()
    cfg = BacktestConfig(
        symbol="KO",
        retrain_freq_days=63,
        min_train_days=300,
        ensemble_builder="oof",
        feature_version="v3",
    )
    engine = BacktestEngine(cfg, dc)
    results = engine.run_multi({"full": full_signal, "buy_only": buy_only})

    assert set(results.keys()) == {"full", "buy_only"}
    full_r = results["full"]
    buy_r = results["buy_only"]
    # raw_predictions MUST match byte-for-byte — this is the whole point of
    # run_multi existing.
    assert full_r.raw_predictions == buy_r.raw_predictions, \
        "run_multi strategies must share identical raw_predictions"
    # z_scores ditto (computed from same history).
    assert full_r.z_scores == buy_r.z_scores
    # Dates ditto.
    assert full_r.dates == buy_r.dates
    # Buy-only never sells.
    assert buy_r.sells == 0
    print(f"  [basic] run_multi byte-identical OK: full={full_r.num_trades} trades, "
          f"buy_only={buy_r.num_trades} trades")


def test_n_jobs_one_on_fitted_estimators() -> None:
    """Every base estimator in the fitted V3 ensemble must have n_jobs==1.
    Wright note #5: regression guard against accidental override."""
    dc = _load_small_ko_frame()
    cfg = BacktestConfig(
        symbol="KO",
        retrain_freq_days=63,
        min_train_days=300,
        feature_version="v3",
    )
    engine = BacktestEngine(cfg, dc)

    # Intercept the engine's model after first training: run a one-shot
    # config so there's exactly one trained model to inspect, then run full
    # walk-forward to confirm during retrains as well.
    cfg_oneshot = BacktestConfig(
        symbol="KO",
        retrain_freq_days=None,  # one-shot
        feature_version="v3",
    )
    # Reach into engine internals by running the one-shot path manually.
    engine_os = BacktestEngine(cfg_oneshot, dc)
    result_os = engine_os.run(full_signal)
    assert result_os.period_days > 0

    # Now do walk-forward and check the final fitted model (retained in the
    # inner loop). Because run_multi doesn't expose the model directly, we
    # assert that the engine ran to completion without the internal
    # _assert_n_jobs_one raising.
    result_wf = engine.run(full_signal)
    assert result_wf.period_days > 0
    print("  [basic] n_jobs==1 regression guard passed on both one-shot and "
          f"walk-forward ({result_wf.period_days}d)")


def run_all() -> int:
    failed = 0
    for test in (
        test_run_single_strategy,
        test_run_multi_shares_predictions,
        test_n_jobs_one_on_fitted_estimators,
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
    print("test_backtest_engine_basic")
    rc = run_all()
    sys.exit(1 if rc else 0)
