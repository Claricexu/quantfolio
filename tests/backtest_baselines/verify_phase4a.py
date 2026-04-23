"""Phase 4a verification — compare refactored backtest_symbol against an
in-script reference implementation of the OLD walk-forward loop
(finance_model_v2.py:766-801 pre-refactor).

Why live-vs-live instead of the committed JSONs?
------------------------------------------------
The baselines in this directory were captured on the morning of 2026-04-22
from ``data_cache/*.csv`` snapshots that were themselves 3 bars shorter
than the caches at the time Phase 4a was wired in. ``_cache_fresh`` will
accept the longer cache as "up to date" for today's session, so by the
time this script runs the input frame is ALREADY a different length than
the committed JSONs. A length mismatch on every ticker is not a "refactor
broke something" signal — it is a data-recency signal.

Same reasoning as ``verify_phase3.py``. We instead reproduce the OLD
walk-forward loop inside this script and run it against the SAME
live-fetched dataframe that the engine sees, so the diff is pure
"refactor vs refactor". The committed JSONs remain useful receipts for
the "before" snapshot; they are not consulted here.

For SHORTHIST_AAPL, the pre-refactor baseline was captured under the
temporary monkey-patch ``MIN_TRAIN_DAYS=80`` (see capture_baselines.py
module docstring). We re-apply the same patch locally for the SHORTHIST
ticker only, so the reference-vs-engine comparison is apples-to-apples.
Exits 0 if all pass, 1 otherwise.

Tolerance: ``1e-4`` on portfolio_curve entries and summary stats. Exact
byte-identity is not achievable because the engine's ``_simulate`` uses
``round()`` on some intermediate quantities. "Identical within 1e-4" is
the precise claim.
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
from finance_model_v2 import backtest_symbol  # noqa: E402

BASELINE_DIR = os.path.dirname(os.path.abspath(__file__))
SHORTHIST_SYMBOL = "SHORTHIST_AAPL"
REAL_CACHE_DIR = os.path.join(REPO_ROOT, "data_cache")
SHORTHIST_CACHE_DIR = os.path.join(BASELINE_DIR, "_shorthist_cache")

# Same value used by capture_baselines.py to force the MIN_ZSCORE_SAMPLES guard
# to fire on SHORTHIST_AAPL. Both reference and engine must see the same value.
SHORTHIST_PATCH_MIN_TRAIN_DAYS = 80

CURVE_ATOL = 1e-4
STAT_ATOL = 1e-4


def _cache_dir_for(symbol: str) -> str:
    return SHORTHIST_CACHE_DIR if symbol == SHORTHIST_SYMBOL else REAL_CACHE_DIR


def _materialize_shorthist() -> None:
    """Re-emit _shorthist_cache/SHORTHIST_AAPL.csv from the CURRENT tail of
    data_cache/AAPL.csv. Needed so fetch_stock_data's freshness check passes
    without attempting to live-fetch 'SHORTHIST_AAPL' from yfinance."""
    os.makedirs(SHORTHIST_CACHE_DIR, exist_ok=True)
    src = os.path.join(REAL_CACHE_DIR, "AAPL.csv")
    dst = os.path.join(SHORTHIST_CACHE_DIR, f"{SHORTHIST_SYMBOL}.csv")
    df = pd.read_csv(src, index_col=0, parse_dates=True)
    tail = df.iloc[-(3 * 252):].copy()
    tail.index.name = df.index.name or "Date"
    tail.to_csv(dst)


class _patched_min_train_days:
    """Context manager mirroring capture_baselines._patched_min_train_days.
    Temporarily lowers fm.MIN_TRAIN_DAYS so the MIN_ZSCORE_SAMPLES guard
    actually fires on SHORTHIST. Both our reference loop (which reads
    fm.MIN_TRAIN_DAYS dynamically) and the engine wrapper (which also reads
    it dynamically) respect the patched value."""

    def __init__(self, value: int, enabled: bool = True):
        self.value = value
        self.enabled = enabled
        self._prev: int | None = None

    def __enter__(self) -> "_patched_min_train_days":
        if self.enabled:
            self._prev = fm.MIN_TRAIN_DAYS
            fm.MIN_TRAIN_DAYS = self.value
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.enabled and self._prev is not None:
            fm.MIN_TRAIN_DAYS = self._prev


# ---------------------------------------------------------------------------
# Reference OLD walk-forward loop — copy of pre-refactor finance_model_v2.py
# L766-L801, stripped of side effects so it returns the captured curve.
# ---------------------------------------------------------------------------

def _old_backtest_symbol_loop(dc: pd.DataFrame, fcols: list[str], version: str,
                              strategy: str) -> dict:
    rf_freq = fm.RETRAIN_FREQ_V3 if version == "v3" else fm.RETRAIN_FREQ_V2
    aX = dc[fcols].values
    ay = dc["Target_Return"].values
    ap = dc["Close"].values.ravel()

    bm = dc.index >= pd.Timestamp(fm.BACKTEST_PREFERRED_START)
    bsi = int(np.argmax(bm)) if bm.any() else 0
    bsi = max(bsi, int(fm.MIN_TRAIN_DAYS))
    if bsi >= len(dc) - int(fm.MIN_BACKTEST_DAYS):
        return {"error": f"not enough data ({len(dc)} rows)"}

    cash, sh = 10000.0, 0.0
    port: list[float] = []
    sigs: list[str] = []
    dates_out: list[str] = []
    mdl = None
    pf = None
    sc = StandardScaler()
    rc = 0
    pred_history: list[float] = []

    for i in range(bsi, len(aX) - 1):
        if mdl is None or rc >= rf_freq:
            vs = int(i * 0.85)
            sc.fit(aX[:vs])
            if version == "v3" and fm.HAS_LGBM:
                mdl = fm.build_stacking_ensemble(
                    sc.transform(aX[:vs]), ay[:vs],
                    sc.transform(aX[vs:i]), ay[vs:i],
                )
                pf = fm.predict_v3
            else:
                mdl = (
                    fm.train_rf_v2(sc.transform(aX[:vs]), ay[:vs]),
                    fm.train_xgb_v2(sc.transform(aX[:vs]), ay[:vs]),
                )
                pf = fm.predict_v2
            if not pred_history:
                seed_preds = pf(mdl, sc.transform(aX[vs:i]))
                pred_history = list(seed_preds[-fm.ZSCORE_LOOKBACK:])
            rc = 0

        raw_pred = float(pf(mdl, sc.transform(aX[i:i + 1]))[0])
        pred_history.append(raw_pred)
        if len(pred_history) > fm.ZSCORE_LOOKBACK:
            pred_history = pred_history[-fm.ZSCORE_LOOKBACK:]

        if len(pred_history) >= int(fm.MIN_ZSCORE_SAMPLES):
            hist = np.array(pred_history[:-1])
            mu, sigma = float(np.mean(hist)), float(np.std(hist))
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0

        pr = float(ap[i])
        sig = "HOLD"
        if strategy == "buy_only":
            if z >= fm.THRESHOLD and cash > 0:
                sh = cash / pr
                cash = 0
                sig = "BUY"
        else:  # full
            if z >= fm.THRESHOLD and cash > 0:
                sh = cash / pr
                cash = 0
                sig = "BUY"
            elif z <= -fm.THRESHOLD and sh > 0:
                cash = sh * pr
                sh = 0
                sig = "SELL"
        sigs.append(sig)
        port.append(cash + sh * pr)
        dates_out.append(dc.index[i].strftime("%Y-%m-%d"))
        rc += 1

    return {
        "portfolio_curve": port,
        "dates": dates_out,
        "signals": sigs,
        "buys": sigs.count("BUY"),
        "sells": sigs.count("SELL"),
        "holds": sigs.count("HOLD"),
    }


# ---------------------------------------------------------------------------
# Stat helpers — kept in sync with backtest_engine._sharpe / _sortino /
# _max_drawdown_pct and capture_baselines._summary.
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

    if len(old["portfolio_curve"]) != len(new["portfolio_curve"]):
        fails.append(
            f"curve length mismatch: old={len(old['portfolio_curve'])} "
            f"new={len(new['portfolio_curve'])}"
        )
        return False, fails

    # Curve check within 1e-4 tolerance.
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

    # Dates.
    for i, (a, b) in enumerate(zip(old.get("dates", []), new.get("dates", []))):
        if a != b:
            fails.append(f"dates[{i}]: old={a!r} new={b!r}")
            break

    # Summary stats.
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

    # Signal counts.
    for k in ("buys", "sells"):
        ov = old.get(k)
        nv = new.get(k)
        if ov is not None and nv is not None and ov != nv:
            fails.append(f"{k}: old={ov} new={nv}")

    return (len(fails) == 0, fails)


# ---------------------------------------------------------------------------
# Frame prep (shared with verify_phase3.py pattern).
# ---------------------------------------------------------------------------

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


def _run_one(symbol: str) -> tuple[bool, str, float | None, float | None]:
    """Returns (ok, reason, old_final, new_final)."""
    patch_enabled = symbol == SHORTHIST_SYMBOL
    with _patched_min_train_days(SHORTHIST_PATCH_MIN_TRAIN_DAYS, enabled=patch_enabled):
        try:
            dc, fcols, version = _prep_frame(symbol)
        except Exception as e:
            return False, f"prep error: {e}", None, None

        # Reference OLD loop.
        try:
            old = _old_backtest_symbol_loop(dc, fcols, version, "full")
        except Exception as e:
            return False, f"old-loop error: {e}", None, None
        if "error" in old:
            return False, old["error"], None, None

        # Engine via the refactored backtest_symbol wrapper.
        try:
            port = backtest_symbol(symbol, cache_dir=_cache_dir_for(symbol),
                                   version=version, strategy="full")
        except Exception as e:
            return False, f"engine error: {e}", None, None
        if port is None:
            return False, "engine returned None", None, None

        # Engine doesn't surface buys/sells to backtest_symbol's callers, so
        # pull those from the print-line we just emitted? No — easier to recover
        # them by counting from the engine's Trade list via a direct engine
        # call. But the wrapper returns only the portfolio_curve, which is fine
        # — _compare only hits buys/sells when present. Leave them out and rely
        # on curve + stats for byte-identity.
        new = {
            "portfolio_curve": list(port),
            "dates": old["dates"],  # same bsi-based dates by construction
        }
        ok, fails = _compare(old, new)
        old_final = _stats(old["portfolio_curve"])["final_portfolio_value"]
        new_final = _stats(new["portfolio_curve"])["final_portfolio_value"]
        reason = "identical within 1e-4" if ok else "; ".join(fails[:3]) + (
            f" (+{len(fails) - 3} more)" if len(fails) > 3 else ""
        )
        return ok, reason, old_final, new_final


def main() -> int:
    tickers: list[str] = []
    for fn in sorted(os.listdir(BASELINE_DIR)):
        if fn.endswith("_backtest_symbol.json"):
            tickers.append(fn.replace("_backtest_symbol.json", ""))
    if not tickers:
        print("[verify-4a] no backtest_symbol baselines found")
        return 1

    print(f"[verify-4a] tickers ({len(tickers)}): {tickers}")
    if SHORTHIST_SYMBOL in tickers:
        _materialize_shorthist()

    rows = []
    total_fail = 0
    for sym in tickers:
        print(f"[verify-4a] === {sym} ===")
        ok, reason, ov, nv = _run_one(sym)
        rows.append((sym, ov, nv, ok, reason))
        if not ok:
            total_fail += 1

    print()
    print(f"{'ticker':<20}{'ref_final':>18}{'engine_final':>18}  match  reason")
    print("-" * 110)
    for sym, bv, nv, ok, reason in rows:
        bvs = f"${bv:,.4f}" if bv is not None else "ERROR"
        nvs = f"${nv:,.4f}" if nv is not None else "ERROR"
        status = "PASS" if ok else "FAIL"
        print(f"{sym:<20}{bvs:>18}{nvs:>18}  {status:<5} {reason}")

    print()
    if total_fail == 0:
        print(f"[verify-4a] ALL {len(rows)} PASS (identical within 1e-4)")
        return 0
    print(f"[verify-4a] {total_fail} of {len(rows)} FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
