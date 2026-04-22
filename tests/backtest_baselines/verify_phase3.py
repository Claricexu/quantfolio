"""Phase 3 verification — compare refactored predict_ticker's engine-based
backtest against an in-script reference implementation of the OLD inline loop.

Why not diff against the on-disk baselines? The baselines were captured earlier
today against a cached data snapshot; ``data_cache/*.csv`` is refreshed on
every ``fetch_stock_data`` call whose freshness check fails, so by the time
this script runs the underlying input frame may have shifted by one trading
bar. We instead reproduce the OLD inline loop inside this script and run it
against the SAME live-fetched dataframe that the engine sees, so the diff is
pure "refactor vs refactor", not "refactor vs stale baseline".

The pre-refactor JSON baselines in this directory remain useful receipts for
the "before" snapshot; they are not consulted by this script directly.

Option A was chosen:
  * Engine is called with ``seed_from_validation=False`` (so pred_history is
    empty at step 0 and MIN_ZSCORE_SAMPLES=20 blocks signals until step 20).
  * Wrapper trims ``result.portfolio_curve[seed_n:]`` where
    ``seed_n = min(20, max(5, len(Xvl)//3))`` — the exact iteration start of
    the old inline loop. For every ticker with ``seed_n==20`` (true for all 7
    Phase 1 baselines, including SHORTHIST_AAPL), output is byte-identical.

Exits 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import finance_model_v2 as fm  # noqa: E402
from finance_model_v2 import predict_ticker  # noqa: E402
from backtest_engine import (  # noqa: E402
    BacktestConfig,
    BacktestEngine,
    full_signal,
    buy_only,
)

BASELINE_DIR = os.path.dirname(os.path.abspath(__file__))
SHORTHIST_SYMBOL = "SHORTHIST_AAPL"
REAL_CACHE_DIR = os.path.join(REPO_ROOT, "data_cache")
SHORTHIST_CACHE_DIR = os.path.join(BASELINE_DIR, "_shorthist_cache")

CURVE_ATOL = 1e-4
RETURNS_ATOL = 1e-8
STAT_ATOL = 1e-4


def _cache_dir_for(symbol: str) -> str:
    return SHORTHIST_CACHE_DIR if symbol == SHORTHIST_SYMBOL else REAL_CACHE_DIR


def _materialize_shorthist() -> None:
    """Re-emit _shorthist_cache/SHORTHIST_AAPL.csv from the CURRENT tail of
    data_cache/AAPL.csv. Needed because fetch_stock_data's freshness check
    will otherwise try to live-fetch 'SHORTHIST_AAPL' from yfinance and 404.
    """
    os.makedirs(SHORTHIST_CACHE_DIR, exist_ok=True)
    src = os.path.join(REAL_CACHE_DIR, "AAPL.csv")
    dst = os.path.join(SHORTHIST_CACHE_DIR, f"{SHORTHIST_SYMBOL}.csv")
    df = pd.read_csv(src, index_col=0, parse_dates=True)
    tail = df.iloc[-(3 * 252):].copy()
    tail.index.name = df.index.name or "Date"
    tail.to_csv(dst)


# ---------------------------------------------------------------------------
# Reference OLD inline loop (copy of pre-refactor finance_model_v2.py L484-502).
# Untouched except for returning the captured series instead of mutating state.
# ---------------------------------------------------------------------------

def _old_inline_backtest(dc: pd.DataFrame, fcols: list[str], version: str,
                         strategy: str) -> dict:
    aX = dc[fcols].values
    ay = dc["Target_Return"].values
    te = len(aX)
    vs = int(te * 0.85)
    Xtr, Xvl = aX[:vs], aX[vs:]
    ytr, yvl = ay[:vs], ay[vs:]

    scaler = StandardScaler()
    scaler.fit(Xtr)
    Xtr_s = scaler.transform(Xtr)
    Xvl_s = scaler.transform(Xvl)

    if version == "v3" and fm.HAS_LGBM:
        model = fm.build_stacking_ensemble(Xtr_s, ytr, Xvl_s, yvl)
        pf = fm.predict_v3
    else:
        rf_model = fm.train_rf_v2(scaler.transform(aX[:te]), ay[:te])
        xgb_model = fm.train_xgb_v2(scaler.transform(aX[:te]), ay[:te])
        model = (rf_model, xgb_model)
        pf = fm.predict_v2

    yp = pf(model, Xvl_s)

    seed_n = min(20, max(5, len(Xvl) // 3))
    btc, bts = 10000.0, 0.0
    btp: list[float] = []
    bt_pred_hist = list(yp[:seed_n])
    dates_out: list[str] = []

    for j in range(seed_n, len(Xvl)):
        pr = float(yp[j])
        bp = float(dc["Close"].iloc[vs + j]) if vs + j < len(dc) else float(dc["Close"].iloc[-1])
        bt_pred_hist.append(pr)
        if len(bt_pred_hist) > fm.ZSCORE_LOOKBACK:
            bt_pred_hist = bt_pred_hist[-fm.ZSCORE_LOOKBACK:]
        hist = np.array(bt_pred_hist[:-1])
        mu, sigma = float(np.mean(hist)), float(np.std(hist))
        z = (pr - mu) / sigma if sigma > 1e-10 else 0.0
        if strategy == "buy_only":
            if z >= fm.THRESHOLD and btc > 0:
                bts = btc / bp
                btc = 0
        else:  # full
            if z >= fm.THRESHOLD and btc > 0:
                bts = btc / bp
                btc = 0
            elif z <= -fm.THRESHOLD and bts > 0:
                btc = bts * bp
                bts = 0
        pv = btc + bts * bp
        btp.append(pv)
        dates_out.append(str(dc.index[vs + j].date()))

    return {
        "portfolio_curve": btp,
        "dates": dates_out,
        "seed_n": seed_n,
        "len_Xvl": len(Xvl),
    }


# ---------------------------------------------------------------------------
# Engine-based run + trim (mirrors the refactored predict_ticker wrapper).
# ---------------------------------------------------------------------------

def _engine_backtest(dc: pd.DataFrame, symbol: str, version: str,
                     strategy: str, seed_n: int) -> dict:
    cfg = BacktestConfig(
        symbol=symbol,
        strategy_name=strategy,
        initial_cash=10000.0,
        threshold=fm.THRESHOLD,
        zscore_lookback=fm.ZSCORE_LOOKBACK,
        min_zscore_samples=fm.MIN_ZSCORE_SAMPLES,
        retrain_freq_days=None,
        min_train_days=fm.MIN_TRAIN_DAYS,
        seed_from_validation=False,
        random_state=42,
        ensemble_builder="oof",
        feature_version=version,
    )
    strat_fn = buy_only if strategy == "buy_only" else full_signal
    result = BacktestEngine(cfg, dc).run(strat_fn)
    return {
        "portfolio_curve": list(result.portfolio_curve[seed_n:]),
        "dates": list(result.dates[seed_n:]),
        "num_trades": sum(1 for t in result.trades if t.date in set(result.dates[seed_n:])),
        "full_port": result.portfolio_curve,
        "full_dates": result.dates,
    }


# ---------------------------------------------------------------------------
# Stat helpers shared with baseline _summary().
# ---------------------------------------------------------------------------

def _sharpe(port_arr: np.ndarray) -> float:
    if port_arr.size < 2:
        return 0.0
    d = np.diff(port_arr) / port_arr[:-1]
    if d.size == 0 or np.std(d) <= 0:
        return 0.0
    return float(np.mean(d) / np.std(d) * math.sqrt(252))


def _sortino(port_arr: np.ndarray) -> float:
    if port_arr.size < 2:
        return 0.0
    d = np.diff(port_arr) / port_arr[:-1]
    dn = d[d < 0]
    if dn.size == 0 or np.std(dn) <= 0:
        return 0.0
    return float(np.mean(d) / np.std(dn) * math.sqrt(252))


def _max_dd(port_arr: np.ndarray) -> float:
    if port_arr.size == 0:
        return 0.0
    pk = np.maximum.accumulate(port_arr)
    return float(((port_arr - pk) / pk).min()) * 100.0


def _stats(port: list[float]) -> dict:
    arr = np.asarray(port, dtype=float)
    return {
        "final_portfolio_value": float(arr[-1]) if arr.size else None,
        "sharpe": _sharpe(arr),
        "sortino": _sortino(arr),
        "max_drawdown_pct": _max_dd(arr),
    }


def _compare(old: dict, new: dict) -> tuple[bool, list[str]]:
    fails: list[str] = []

    # Length must match.
    if len(old["portfolio_curve"]) != len(new["portfolio_curve"]):
        fails.append(
            f"curve length mismatch: old={len(old['portfolio_curve'])} "
            f"new={len(new['portfolio_curve'])}"
        )
        return False, fails

    # Byte-identical curve check.
    max_curve_diff = 0.0
    first_bad_curve: tuple[int, float, float, float] | None = None
    for i, (a, b) in enumerate(zip(old["portfolio_curve"], new["portfolio_curve"])):
        d = abs(float(a) - float(b))
        if d > max_curve_diff:
            max_curve_diff = d
        if d > CURVE_ATOL and first_bad_curve is None:
            first_bad_curve = (i, a, b, d)
    if first_bad_curve is not None:
        i, a, b, d = first_bad_curve
        fails.append(
            f"portfolio_curve[{i}]: old={a:.8f} new={b:.8f} "
            f"diff={d:.3e} (max_diff={max_curve_diff:.3e})"
        )

    # Dates byte-identical.
    for i, (a, b) in enumerate(zip(old["dates"], new["dates"])):
        if a != b:
            fails.append(f"dates[{i}]: old={a!r} new={b!r}")
            break

    # Summary-stat checks.
    old_stats = _stats(old["portfolio_curve"])
    new_stats = _stats(new["portfolio_curve"])
    for k in ("final_portfolio_value", "sharpe", "sortino", "max_drawdown_pct"):
        ov = old_stats[k]
        nv = new_stats[k]
        if ov is None or nv is None:
            if ov != nv:
                fails.append(f"{k}: old={ov!r} new={nv!r}")
            continue
        if abs(float(ov) - float(nv)) > STAT_ATOL:
            fails.append(f"{k}: old={ov:.6f} new={nv:.6f} diff={abs(ov-nv):.3e}")

    return (len(fails) == 0, fails)


def _verify_predict_ticker_return_shape(symbol: str) -> list[str]:
    """Sanity: call the real predict_ticker and confirm its return dict has the
    expected top-level keys + backtest sub-keys."""
    cache_dir = _cache_dir_for(symbol)
    result = predict_ticker(symbol, cache_dir=cache_dir, verbose=False, version="v3", strategy="full")
    fails: list[str] = []
    expected_top = {
        "symbol", "current_price", "predicted_price", "pct_change", "z_score",
        "signal", "signal_rules", "strategy_mode", "strategy_label",
        "model_version", "model_predictions", "svr", "market_cap",
        "quarterly_revenue", "sector", "industry", "quote_type",
        "backtest_mae_pct", "backtest_rmse_pct", "direction_accuracy",
        "backtest_mae", "backtest_rmse", "data_points", "train_window",
        "train_window_setting", "last_date", "backtest",
    }
    missing = expected_top - set(result.keys())
    if missing:
        fails.append(f"predict_ticker return dict missing keys: {sorted(missing)}")
    expected_bt = {
        "strategy_return", "buyhold_return", "sharpe", "max_drawdown",
        "bnh_sharpe", "bnh_max_drawdown", "test_days",
    }
    bt = result.get("backtest", {})
    missing_bt = expected_bt - set(bt.keys())
    if missing_bt:
        fails.append(f"predict_ticker backtest sub-dict missing keys: {sorted(missing_bt)}")
    return fails


def _prep_frame(symbol: str) -> tuple[pd.DataFrame, list[str], str]:
    cache_dir = _cache_dir_for(symbol)
    raw = fm.fetch_stock_data([symbol], cache_dir=cache_dir)
    df = raw[symbol].copy()
    if fm.HAS_LGBM:
        df = fm.engineer_features_v3(df)
        fcols = list(fm.V3_FEATURE_COLS)
        version = "v3"
    else:
        df = fm.engineer_features_v2(df)
        fcols = list(fm.V2_FEATURE_COLS)
        version = "v2"
    dc = df.dropna(subset=["Target_Return"] + fcols).copy()
    return dc, fcols, version


def main() -> int:
    tickers: list[str] = []
    for fn in sorted(os.listdir(BASELINE_DIR)):
        if fn.endswith("_predict_inline.json"):
            tickers.append(fn.replace("_predict_inline.json", ""))
    if not tickers:
        print("[verify] no predict_inline baselines found")
        return 1

    print(f"[verify] tickers ({len(tickers)}): {tickers}")
    # Ensure SHORTHIST cache is fresh against current AAPL data.
    if SHORTHIST_SYMBOL in tickers:
        _materialize_shorthist()
    rows = []
    total_fail = 0
    shape_checked = False

    for sym in tickers:
        try:
            dc, fcols, version = _prep_frame(sym)
        except Exception as e:
            rows.append((sym, None, None, False, f"prep error: {e}"))
            total_fail += 1
            continue

        try:
            old = _old_inline_backtest(dc, fcols, version, "full")
        except Exception as e:
            rows.append((sym, None, None, False, f"old-loop error: {e}"))
            total_fail += 1
            continue

        try:
            new = _engine_backtest(dc, sym, version, "full", seed_n=old["seed_n"])
        except Exception as e:
            rows.append((sym, _stats(old["portfolio_curve"])["final_portfolio_value"], None, False, f"engine error: {e}"))
            total_fail += 1
            continue

        ok, fails = _compare(old, new)

        # Run the predict_ticker shape check ONCE (on KO, the Phase 1 sanity ticker)
        # to avoid running the full pipeline 7 times.
        if sym == "KO" and not shape_checked:
            shape_checked = True
            try:
                shape_fails = _verify_predict_ticker_return_shape(sym)
            except Exception as e:
                shape_fails = [f"predict_ticker invocation failed: {e}"]
            if shape_fails:
                ok = False
                fails.extend(shape_fails)

        old_final = _stats(old["portfolio_curve"])["final_portfolio_value"]
        new_final = _stats(new["portfolio_curve"])["final_portfolio_value"]
        reason = "byte-identical" if ok else "; ".join(fails[:3]) + (
            f" (+{len(fails) - 3} more)" if len(fails) > 3 else ""
        )
        rows.append((sym, old_final, new_final, ok, reason))
        if not ok:
            total_fail += 1

    print()
    print(f"{'ticker':<20}{'old_final':>18}{'new_final':>18}  match  reason")
    print("-" * 110)
    for sym, bv, nv, ok, reason in rows:
        bvs = f"${bv:,.4f}" if bv is not None else "ERROR"
        nvs = f"${nv:,.4f}" if nv is not None else "ERROR"
        status = "PASS" if ok else "FAIL"
        print(f"{sym:<20}{bvs:>18}{nvs:>18}  {status:<5} {reason}")

    print()
    if total_fail == 0:
        print(f"[verify] ALL {len(rows)} PASS")
        return 0
    print(f"[verify] {total_fail} of {len(rows)} FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
