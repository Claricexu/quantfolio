"""Edge-case tests for BacktestEngine.

Covers the invariants from PHASE2_DESIGN.md Section 7:
    1. Empty predictions -> ValueError
    2. Price gaps (non-monotonic index) -> ValueError
    3. All-zero predictions -> all HOLD (via deterministic strategy_fn)
    4. <min_zscore_samples -> HOLD
    5. Single-period data -> ValueError (not enough to run)

Uses synthetic fake-pretrained-model plumbing to avoid calling the real
ensembles, so the test runs in milliseconds.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import numpy as np
import pandas as pd

import finance_model_v2 as fm
from backtest_engine import (
    BacktestConfig,
    BacktestEngine,
    Context,
    buy_hold,
    full_signal,
)


def _make_synth_frame(n_rows: int, gap_at: int | None = None, monotonic: bool = True) -> pd.DataFrame:
    """Build a synthetic DF that PASSES engine._validate_input except where
    tweaked. Requires all V3_FEATURE_COLS + Target_Return + Close."""
    rng = np.random.default_rng(seed=42)
    cols = list(fm.V3_FEATURE_COLS) + ["Target_Return", "Close"]
    data = {c: rng.standard_normal(n_rows) for c in cols}
    data["Close"] = 100.0 + np.cumsum(rng.standard_normal(n_rows) * 0.5)
    idx = pd.date_range("2020-01-01", periods=n_rows, freq="B")
    idx = pd.DatetimeIndex(idx)
    if gap_at is not None and 0 < gap_at < n_rows:
        # Shift post-gap dates forward by 30 days to create a discontinuity.
        # Index stays monotonic but is "gapped" — engine accepts monotonic
        # gapped; we use non-monotonic below via reindex shuffle.
        dates = list(idx)
        dates = dates[:gap_at] + [d + pd.Timedelta(days=30) for d in dates[gap_at:]]
        idx = pd.DatetimeIndex(dates)
    df = pd.DataFrame(data, index=idx)
    if not monotonic:
        # Swap two adjacent rows to break monotonicity.
        new_idx = list(df.index)
        new_idx[2], new_idx[3] = new_idx[3], new_idx[2]
        df.index = pd.DatetimeIndex(new_idx)
    return df


def test_empty_frame_raises() -> None:
    df = pd.DataFrame()
    cfg = BacktestConfig(symbol="X", retrain_freq_days=None, feature_version="v3")
    try:
        BacktestEngine(cfg, df)
    except ValueError as exc:
        assert "insufficient data" in str(exc) or "DatetimeIndex" in str(exc), \
            f"expected ValueError about empty/no-index; got: {exc}"
        print(f"  [edge] empty frame -> ValueError OK ({exc.__class__.__name__})")
        return
    raise AssertionError("expected ValueError on empty DataFrame")


def test_non_monotonic_index_raises() -> None:
    df = _make_synth_frame(200, monotonic=False)
    cfg = BacktestConfig(symbol="X", retrain_freq_days=None, feature_version="v3")
    try:
        BacktestEngine(cfg, df)
    except ValueError as exc:
        assert "non-monotonic" in str(exc), f"expected 'non-monotonic' message, got: {exc}"
        assert "engineer_features_v3 first" in str(exc), \
            "error must include the canonical Wright hint"
        print(f"  [edge] non-monotonic index -> ValueError OK")
        return
    raise AssertionError("expected ValueError on non-monotonic index")


def test_missing_column_raises() -> None:
    df = _make_synth_frame(200)
    df = df.drop(columns=["Close"])
    cfg = BacktestConfig(symbol="X", retrain_freq_days=None, feature_version="v3")
    try:
        BacktestEngine(cfg, df)
    except ValueError as exc:
        assert "missing required columns" in str(exc), f"got: {exc}"
        print(f"  [edge] missing column -> ValueError OK")
        return
    raise AssertionError("expected ValueError on missing column")


def test_nans_in_required_cols_raise() -> None:
    df = _make_synth_frame(200)
    df.iloc[5, df.columns.get_loc("Target_Return")] = np.nan
    cfg = BacktestConfig(symbol="X", retrain_freq_days=None, feature_version="v3")
    try:
        BacktestEngine(cfg, df)
    except ValueError as exc:
        assert "NaN" in str(exc) or "contains NaNs" in str(exc), f"got: {exc}"
        print(f"  [edge] NaNs in required cols -> ValueError OK")
        return
    raise AssertionError("expected ValueError on NaN in required cols")


def test_too_short_for_oneshot_raises() -> None:
    """99 rows < 100 minimum for one-shot training."""
    df = _make_synth_frame(99)
    cfg = BacktestConfig(symbol="X", retrain_freq_days=None, feature_version="v3")
    engine = BacktestEngine(cfg, df)
    try:
        engine.run(full_signal)
    except ValueError as exc:
        assert "insufficient data" in str(exc), f"got: {exc}"
        print(f"  [edge] <100 rows one-shot -> ValueError OK")
        return
    raise AssertionError("expected ValueError on too-short one-shot")


def test_too_short_for_walkforward_raises() -> None:
    """walk-forward with bsi >= len-1 should raise."""
    df = _make_synth_frame(200)
    cfg = BacktestConfig(
        symbol="X",
        retrain_freq_days=63,
        min_train_days=500,  # > len(df), forces insufficient data
        feature_version="v3",
    )
    engine = BacktestEngine(cfg, df)
    try:
        engine.run(full_signal)
    except ValueError as exc:
        assert "insufficient data" in str(exc), f"got: {exc}"
        print(f"  [edge] bsi>=len for walk-forward -> ValueError OK")
        return
    raise AssertionError("expected ValueError on too-short walk-forward")


def test_min_zscore_samples_forces_hold() -> None:
    """During the first min_zscore_samples-1 steps, samples_ready=False so
    every built-in strategy returns HOLD."""
    # Use a tiny synthetic simulation by stubbing the engine's internals.
    # We call _simulate directly with a fixed list of predictions so we don't
    # touch the real ensembles.
    cfg = BacktestConfig(
        symbol="X",
        retrain_freq_days=None,
        feature_version="v3",
        min_zscore_samples=20,
        zscore_lookback=126,
    )
    # Build an engine around a dummy frame that passes validation (just so
    # construction succeeds) — we won't call run_multi, only _simulate.
    df = _make_synth_frame(200)
    engine = BacktestEngine(cfg, df)

    # Feed 30 steps of predictions, no seed history.
    raw_preds = [0.02] * 30
    dates = [f"2020-01-{i:02d}" for i in range(1, 31)]
    prices = [100.0 + i for i in range(30)]
    results = engine._simulate(
        dates=dates,
        prices=prices,
        raw_preds=raw_preds,
        pred_history=[],
        strategy_fns={"full": full_signal},
    )
    r = results["full"]
    # For the first min_zscore_samples (20) steps, the guard blocks — BUT
    # history.append happens BEFORE the >= check, so the guard fires while
    # len(history) < 20. On step 19 (index 19), history len=20 so guard passes.
    # First 19 steps must be HOLD.
    assert all(s == "HOLD" for s in r.signals[:19]), \
        f"expected all HOLD before samples_ready, got {r.signals[:20]}"
    # All z-scores before sample-ready are 0.0.
    assert all(z == 0.0 for z in r.z_scores[:19]), \
        f"expected all z_score==0.0 before samples_ready, got {r.z_scores[:20]}"
    print("  [edge] <min_zscore_samples -> all HOLD OK (first 19 steps)")


def test_all_zero_predictions_means_all_hold() -> None:
    """All-zero raw predictions -> z-score is 0, so built-ins return HOLD."""
    cfg = BacktestConfig(
        symbol="X",
        retrain_freq_days=None,
        feature_version="v3",
        min_zscore_samples=20,
    )
    df = _make_synth_frame(200)
    engine = BacktestEngine(cfg, df)
    # 50 steps of zero predictions; seed with enough history to skip the guard.
    raw_preds = [0.0] * 50
    dates = [f"2020-01-{(i % 28) + 1:02d}" for i in range(50)]
    prices = [100.0 + i for i in range(50)]
    seed = [0.0] * 25  # enough to trip samples_ready immediately
    results = engine._simulate(
        dates=dates, prices=prices, raw_preds=raw_preds,
        pred_history=seed, strategy_fns={"full": full_signal},
    )
    r = results["full"]
    assert r.buys == 0 and r.sells == 0, f"expected 0 trades, got buys={r.buys} sells={r.sells}"
    assert all(s == "HOLD" for s in r.signals), "expected all HOLD for zero predictions"
    # Portfolio curve is flat at initial_cash.
    assert all(abs(v - cfg.initial_cash) < 1e-9 for v in r.portfolio_curve), \
        f"expected flat portfolio at {cfg.initial_cash}, got {r.portfolio_curve[:3]}..."
    assert r.sharpe == 0.0
    assert r.max_drawdown_pct == 0.0
    print(f"  [edge] all-zero preds -> all HOLD, flat portfolio OK")


def test_single_period_data() -> None:
    """Exactly one prediction step. Engine shouldn't crash."""
    cfg = BacktestConfig(
        symbol="X",
        retrain_freq_days=None,
        feature_version="v3",
        min_zscore_samples=20,
    )
    df = _make_synth_frame(200)
    engine = BacktestEngine(cfg, df)
    results = engine._simulate(
        dates=["2020-01-01"],
        prices=[100.0],
        raw_preds=[0.05],
        pred_history=[],
        strategy_fns={"full": full_signal},
    )
    r = results["full"]
    assert r.period_days == 1
    assert len(r.portfolio_curve) == 1
    assert r.portfolio_curve[0] == cfg.initial_cash  # HOLD (not enough samples)
    assert r.per_period_returns == []
    assert r.signals == ["HOLD"]
    assert r.sharpe == 0.0
    assert r.sortino == 0.0
    assert r.max_drawdown_pct == 0.0
    print(f"  [edge] single-period run OK: port=[{r.portfolio_curve[0]}]")


def test_invalid_strategy_signal_raises() -> None:
    """A strategy callback returning a non-{BUY,SELL,HOLD} value raises."""
    cfg = BacktestConfig(
        symbol="X", retrain_freq_days=None, feature_version="v3",
    )
    df = _make_synth_frame(200)
    engine = BacktestEngine(cfg, df)
    bad = lambda ctx: "NONSENSE"  # type: ignore[return-value]
    try:
        engine._simulate(
            dates=["2020-01-01"],
            prices=[100.0],
            raw_preds=[0.0],
            pred_history=[],
            strategy_fns={"bad": bad},
        )
    except ValueError as exc:
        assert "invalid signal" in str(exc), f"got: {exc}"
        print(f"  [edge] bad strategy signal -> ValueError OK")
        return
    raise AssertionError("expected ValueError on bad strategy signal")


def run_all() -> int:
    failed = 0
    for test in (
        test_empty_frame_raises,
        test_non_monotonic_index_raises,
        test_missing_column_raises,
        test_nans_in_required_cols_raise,
        test_too_short_for_oneshot_raises,
        test_too_short_for_walkforward_raises,
        test_min_zscore_samples_forces_hold,
        test_all_zero_predictions_means_all_hold,
        test_single_period_data,
        test_invalid_strategy_signal_raises,
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
    print("test_backtest_engine_edge_cases")
    rc = run_all()
    sys.exit(1 if rc else 0)
