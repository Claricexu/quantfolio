"""Phase 4b verification — compare refactored backtest_multi_strategy against
an in-script reference implementation of the OLD walk-forward loop
(finance_model_v2.py:827-877 pre-refactor).

Design
------
Same live-vs-live pattern as verify_phase4a.py. The committed JSONs are NOT
consulted; the reference loop is re-implemented here and diffed against the
engine-backed wrapper on the same live-fetched frame.

Two sub-portfolios per ticker (``full`` and ``buy_only``), so each ticker
runs 2 comparisons. 7 tickers × 2 sub-portfolios = 14 comparisons total.

MSFT intentional change
-----------------------
The pre-refactor ``backtest_multi_strategy`` used ``build_stacking_ensemble_fast``
(val-MAE-weighted). The engine's default is ``build_stacking_ensemble`` (OOF),
matching ``backtest_symbol``. For 6 of 7 tickers the two ensemble choices
produce outputs that agree within 1e-4. For MSFT, the two disagree on some data
vintages — which is exactly the C-3 bug this refactor fixes (multi_strategy
previously drifted from backtest_symbol on MSFT/full: $87,367.59 vs $92,159.04
under the Phase-1 data snapshot).

Today's data may or may not surface the MSFT numerical divergence — since the
C-3 fix is structural (both paths now share the same OOF engine config), the
numerical signature depends on whether the val-MAE reference loop happens to
agree with OOF on the current window. Either outcome is acceptable:
  * If MSFT/full DIVERGES vs ref: this is the "INTENTIONAL CHANGE" branch —
    the structural fix visible in numbers. The xcheck confirms engine-internal
    consistency between multi_strategy[full] and backtest_symbol.
  * If MSFT/full AGREES vs ref within 1e-4: today's window doesn't exercise
    the val-MAE-vs-OOF disagreement, but the structural fix is still in
    place and the xcheck still confirms the two paths are in lockstep.
  * For MSFT/buy_only: no interaction with the ensemble-choice issue; expected
    to pass the ref-vs-engine 1e-4 check same as other tickers.

Wrapper rounding (wire contract)
--------------------------------
``backtest_multi_strategy`` rounds portfolio values to 2 decimals (cents) for
the frontend wire contract — see ``finance_model_v2.py`` L870-874. The
reference loop above keeps full float precision. To compare apples-to-apples,
we round every reference curve value to 2 decimals before diffing. The
``backtest_symbol`` wrapper used in the MSFT cross-check does NOT round (it
returns the raw engine curve) — so for xcheck we round the reference side
(bs_port) to 2 decimals before comparing against the rounded multi-strategy
output.

Tolerance: ``1e-4`` on portfolio_curve entries and summary stats, same as 4a.
Sortino is computed from portfolio curves on both sides; after cents-rounding
both curves, sortino matches within float noise at 1e-4.
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
from finance_model_v2 import backtest_multi_strategy, backtest_symbol  # noqa: E402


# ---------------------------------------------------------------------------
# Inlined copy of the pre-Phase-5 ``build_stacking_ensemble_fast`` (deleted
# from finance_model_v2.py in Phase 5 of C-3). The reference loop below uses
# this local copy so it remains faithful to the OLD walk-forward behaviour
# it is meant to mirror.
# ---------------------------------------------------------------------------

def _build_stacking_ensemble_fast(X_train, y_train, X_val, y_val):
    """Val-MAE-weighted stacking (pre-refactor multi_strategy builder).

    3 model fits per retrain (vs 18 for OOF). Retained as a local reference
    so Phase 4b's live-vs-live check still mirrors pre-refactor honest
    behaviour even after the production function was deleted.
    """
    fl = fm.train_lgbm_v3(X_train, y_train, X_val, y_val)
    fx = fm.train_xgb_v3(X_train, y_train, X_val, y_val)
    fr = fm.train_rf_v3(X_train, y_train)
    preds = np.column_stack([fl.predict(X_val), fx.predict(X_val), fr.predict(X_val)])
    mae = np.array([np.mean(np.abs(preds[:, j] - y_val)) for j in range(3)])
    inv_mae = 1.0 / (mae + 0.005)
    weights = inv_mae / inv_mae.sum()
    return {"lgbm": fl, "xgb": fx, "rf": fr, "weights": weights}

BASELINE_DIR = os.path.dirname(os.path.abspath(__file__))
SHORTHIST_SYMBOL = "SHORTHIST_AAPL"
REAL_CACHE_DIR = os.path.join(REPO_ROOT, "data_cache")
SHORTHIST_CACHE_DIR = os.path.join(BASELINE_DIR, "_shorthist_cache")

SHORTHIST_PATCH_MIN_TRAIN_DAYS = 80

CURVE_ATOL = 1e-4
STAT_ATOL = 1e-4


def _cache_dir_for(symbol: str) -> str:
    return SHORTHIST_CACHE_DIR if symbol == SHORTHIST_SYMBOL else REAL_CACHE_DIR


def _materialize_shorthist() -> None:
    os.makedirs(SHORTHIST_CACHE_DIR, exist_ok=True)
    src = os.path.join(REAL_CACHE_DIR, "AAPL.csv")
    dst = os.path.join(SHORTHIST_CACHE_DIR, f"{SHORTHIST_SYMBOL}.csv")
    df = pd.read_csv(src, index_col=0, parse_dates=True)
    tail = df.iloc[-(3 * 252):].copy()
    tail.index.name = df.index.name or "Date"
    tail.to_csv(dst)


class _patched_min_train_days:
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
# L827-877. Produces BOTH sub-portfolios (full + buy_only) in a single pass,
# just like the pre-refactor function did.
#
# Uses build_stacking_ensemble_fast (val-MAE) — the old, honest behavior.
# ---------------------------------------------------------------------------

def _old_multi_strategy_loop(dc: pd.DataFrame, fcols: list[str], version: str) -> dict:
    rf_freq = fm.RETRAIN_FREQ_V3 if version == "v3" else fm.RETRAIN_FREQ_V2
    aX = dc[fcols].values
    ay = dc["Target_Return"].values
    ap = dc["Close"].values.ravel()

    bm = dc.index >= pd.Timestamp(fm.BACKTEST_PREFERRED_START)
    bsi = int(np.argmax(bm)) if bm.any() else 0
    bsi = max(bsi, int(fm.MIN_TRAIN_DAYS))
    if bsi >= len(dc) - int(fm.MIN_BACKTEST_DAYS):
        return {"error": f"not enough data ({len(dc)} rows)"}

    f_cash, f_sh = 10000.0, 0.0
    b_cash, b_sh = 10000.0, 0.0
    f_port: list[float] = []
    b_port: list[float] = []
    f_sigs: list[str] = []
    b_sigs: list[str] = []
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
                mdl = _build_stacking_ensemble_fast(
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
        dates_out.append(dc.index[i].strftime("%Y-%m-%d"))

        # full signal
        if z >= fm.THRESHOLD and f_cash > 0:
            f_sh = f_cash / pr
            f_cash = 0
            f_sigs.append("BUY")
        elif z <= -fm.THRESHOLD and f_sh > 0:
            f_cash = f_sh * pr
            f_sh = 0
            f_sigs.append("SELL")
        else:
            f_sigs.append("HOLD")
        f_port.append(f_cash + f_sh * pr)

        # buy_only
        if z >= fm.THRESHOLD and b_cash > 0:
            b_sh = b_cash / pr
            b_cash = 0
            b_sigs.append("BUY")
        else:
            b_sigs.append("HOLD")
        b_port.append(b_cash + b_sh * pr)

        rc += 1

    return {
        "dates": dates_out,
        "full": {
            "portfolio_curve": f_port,
            "signals": f_sigs,
            "buys": f_sigs.count("BUY"),
            "sells": f_sigs.count("SELL"),
            "holds": f_sigs.count("HOLD"),
        },
        "buy_only": {
            "portfolio_curve": b_port,
            "signals": b_sigs,
            "buys": b_sigs.count("BUY"),
            "sells": b_sigs.count("SELL"),
            "holds": b_sigs.count("HOLD"),
        },
    }


# ---------------------------------------------------------------------------
# Stat helpers
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


def _round_curve(curve: list[float]) -> list[float]:
    """Round a portfolio curve to 2 decimals (cents) to match the wrapper's
    wire contract — ``backtest_multi_strategy`` rounds every portfolio value to
    2 decimals before returning, and ``backtest_symbol``'s engine output is
    likewise rounded here for xcheck apples-to-apples."""
    return [round(float(v), 2) for v in curve]


def _compare(old_curve: list[float], new_curve: list[float]) -> tuple[bool, list[str]]:
    """Compare two portfolio curves. ``old_curve`` is ROUNDED to cents to match
    the wrapper's wire contract before diffing; ``new_curve`` is assumed to be
    already rounded (it comes from the wrapper). All derived stats are then
    computed from the rounded curves on both sides so there is no
    rounded-vs-unrounded mismatch on sharpe/sortino/max_drawdown_pct either.
    """
    fails: list[str] = []
    if len(old_curve) != len(new_curve):
        fails.append(f"curve length mismatch: old={len(old_curve)} new={len(new_curve)}")
        return False, fails

    old_curve_r = _round_curve(old_curve)
    new_curve_r = _round_curve(new_curve)

    max_diff = 0.0
    first_bad: tuple[int, float, float, float] | None = None
    for i, (a, b) in enumerate(zip(old_curve_r, new_curve_r)):
        d = abs(float(a) - float(b))
        if d > max_diff:
            max_diff = d
        if d > CURVE_ATOL and first_bad is None:
            first_bad = (i, a, b, d)
    if first_bad is not None:
        i, a, b, d = first_bad
        fails.append(
            f"portfolio_curve[{i}]: old={a:.8f} new={b:.8f} "
            f"diff={d:.3e} (max_diff={max_diff:.3e})"
        )

    os_ = _stats(old_curve_r)
    ns_ = _stats(new_curve_r)
    for k in ("final_portfolio_value", "sharpe", "sortino", "max_drawdown_pct"):
        ov, nv = os_[k], ns_[k]
        if ov is None or nv is None:
            if ov != nv:
                fails.append(f"{k}: old={ov!r} new={nv!r}")
            continue
        if abs(float(ov) - float(nv)) > STAT_ATOL:
            fails.append(f"{k}: old={ov:.6f} new={nv:.6f} diff={abs(ov-nv):.3e}")

    return (len(fails) == 0, fails)


# ---------------------------------------------------------------------------
# Frame prep
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


def _run_one(symbol: str) -> dict:
    """Returns a dict with per-sub-portfolio results plus the MSFT
    cross-validation when applicable."""
    patch_enabled = symbol == SHORTHIST_SYMBOL
    out: dict = {
        "symbol": symbol,
        "full": {"ok": False, "reason": "", "ref_final": None, "eng_final": None},
        "buy_only": {"ok": False, "reason": "", "ref_final": None, "eng_final": None},
        "msft_crosscheck": None,
    }
    with _patched_min_train_days(SHORTHIST_PATCH_MIN_TRAIN_DAYS, enabled=patch_enabled):
        try:
            dc, fcols, version = _prep_frame(symbol)
        except Exception as e:
            out["full"]["reason"] = f"prep error: {e}"
            out["buy_only"]["reason"] = f"prep error: {e}"
            return out

        # Reference (val-MAE fast ensemble — old honest behavior).
        try:
            ref = _old_multi_strategy_loop(dc, fcols, version)
        except Exception as e:
            out["full"]["reason"] = f"ref error: {e}"
            out["buy_only"]["reason"] = f"ref error: {e}"
            return out
        if "error" in ref:
            out["full"]["reason"] = ref["error"]
            out["buy_only"]["reason"] = ref["error"]
            return out

        # Engine via the refactored wrapper.
        try:
            eng = backtest_multi_strategy(symbol, cache_dir=_cache_dir_for(symbol),
                                          version=version)
        except Exception as e:
            out["full"]["reason"] = f"engine error: {e}"
            out["buy_only"]["reason"] = f"engine error: {e}"
            return out
        if eng is None:
            out["full"]["reason"] = "engine returned None"
            out["buy_only"]["reason"] = "engine returned None"
            return out

        for sub in ("full", "buy_only"):
            ref_curve = ref[sub]["portfolio_curve"]
            eng_curve = eng[sub]["portfolio"]
            ok, fails = _compare(ref_curve, eng_curve)
            ref_final = _stats(ref_curve)["final_portfolio_value"]
            eng_final = _stats(eng_curve)["final_portfolio_value"]
            reason = "identical within 1e-4" if ok else "; ".join(fails[:3]) + (
                f" (+{len(fails) - 3} more)" if len(fails) > 3 else ""
            )
            out[sub]["ok"] = ok
            out[sub]["reason"] = reason
            out[sub]["ref_final"] = ref_final
            out[sub]["eng_final"] = eng_final

        # MSFT cross-check: the engine's multi_strategy[full] must equal the
        # engine's backtest_symbol[full] within 1e-4 (C-3 bug fix).
        if symbol == "MSFT":
            try:
                bs_port = backtest_symbol(symbol, cache_dir=_cache_dir_for(symbol),
                                          version=version, strategy="full")
            except Exception as e:
                out["msft_crosscheck"] = {"ok": False, "reason": f"backtest_symbol error: {e}"}
            else:
                if bs_port is None:
                    out["msft_crosscheck"] = {"ok": False, "reason": "backtest_symbol returned None"}
                else:
                    eng_full_curve = eng["full"]["portfolio"]
                    ok, fails = _compare(list(bs_port), eng_full_curve)
                    msft_bs_final = float(bs_port[-1]) if len(bs_port) else None
                    msft_ms_final = _stats(eng_full_curve)["final_portfolio_value"]
                    reason = "multi_strategy[full] == backtest_symbol within 1e-4" if ok else (
                        "; ".join(fails[:3]) + (f" (+{len(fails) - 3} more)" if len(fails) > 3 else "")
                    )
                    out["msft_crosscheck"] = {
                        "ok": ok,
                        "reason": reason,
                        "bs_final": msft_bs_final,
                        "ms_full_final": msft_ms_final,
                    }

    return out


def main() -> int:
    tickers: list[str] = []
    for fn in sorted(os.listdir(BASELINE_DIR)):
        if fn.endswith("_backtest_symbol.json"):
            tickers.append(fn.replace("_backtest_symbol.json", ""))
    if not tickers:
        print("[verify-4b] no baselines found")
        return 1

    print(f"[verify-4b] tickers ({len(tickers)}): {tickers}")
    if SHORTHIST_SYMBOL in tickers:
        _materialize_shorthist()

    rows = []
    for sym in tickers:
        print(f"[verify-4b] === {sym} ===")
        rows.append(_run_one(sym))

    # Results table
    print()
    print(f"{'ticker':<18}{'sub':<10}{'ref_final':>18}{'eng_final':>18}  match  reason")
    print("-" * 120)

    buy_only_fail = 0
    full_fail = 0
    full_fail_unexpected = 0
    msft_change_seen = False

    for r in rows:
        sym = r["symbol"]
        for sub in ("full", "buy_only"):
            x = r[sub]
            rvs = f"${x['ref_final']:,.4f}" if x["ref_final"] is not None else "ERROR"
            nvs = f"${x['eng_final']:,.4f}" if x["eng_final"] is not None else "ERROR"
            status = "PASS" if x["ok"] else ("CHANGE" if (sym == "MSFT" and sub == "full") else "FAIL")
            print(f"{sym:<18}{sub:<10}{rvs:>18}{nvs:>18}  {status:<6} {x['reason']}")
            if sub == "buy_only" and not x["ok"]:
                buy_only_fail += 1
            if sub == "full" and not x["ok"]:
                full_fail += 1
                if sym == "MSFT":
                    msft_change_seen = True
                else:
                    full_fail_unexpected += 1

    # MSFT cross-check row
    for r in rows:
        if r["symbol"] == "MSFT" and r["msft_crosscheck"] is not None:
            cc = r["msft_crosscheck"]
            bsf = f"${cc.get('bs_final', 0):,.4f}" if cc.get("bs_final") is not None else "ERROR"
            msf = f"${cc.get('ms_full_final', 0):,.4f}" if cc.get("ms_full_final") is not None else "ERROR"
            status = "PASS" if cc["ok"] else "FAIL"
            print(f"{'MSFT':<18}{'xcheck':<10}{bsf:>18}{msf:>18}  {status:<6} {cc['reason']}")

    # Summary
    print()
    print(f"[verify-4b] buy_only: {len(rows) - buy_only_fail}/{len(rows)} PASS within 1e-4")
    print(f"[verify-4b] full:     {len(rows) - full_fail}/{len(rows)} PASS within 1e-4"
          f" (MSFT may diverge vs val-MAE ref on some days — see below)")

    if msft_change_seen:
        print("[verify-4b] MSFT: INTENTIONAL CHANGE — multi_strategy[full] pulled into line with "
              "backtest_symbol (val-MAE ref vs OOF engine); this is the C-3 bug fix.")
    else:
        print("[verify-4b] MSFT: val-MAE ref and OOF engine agree within 1e-4 today — the two "
              "ensemble choices happen to produce the same rounded curve on current data; "
              "C-3 is still structurally fixed (both paths forced through OOF engine).")

    # Cross-check
    msft_cc_ok = True
    for r in rows:
        if r["symbol"] == "MSFT" and r["msft_crosscheck"] is not None:
            msft_cc_ok = bool(r["msft_crosscheck"]["ok"])

    exit_code = 0
    if buy_only_fail > 0:
        print(f"[verify-4b] FAIL: {buy_only_fail} buy_only tickers diverge unexpectedly")
        exit_code = 1
    if full_fail_unexpected > 0:
        print(f"[verify-4b] FAIL: {full_fail_unexpected} non-MSFT full tickers diverge unexpectedly")
        exit_code = 1
    if not msft_cc_ok:
        print("[verify-4b] FAIL: MSFT cross-check (multi_strategy[full] vs backtest_symbol) failed")
        exit_code = 1

    if exit_code == 0:
        print(f"[verify-4b] OK — buy_only {len(rows) - buy_only_fail}/{len(rows)}, "
              f"full {len(rows) - full_fail}/{len(rows)}, MSFT xcheck PASS")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
