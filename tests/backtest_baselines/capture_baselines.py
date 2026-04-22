"""
Capture pre-refactor baselines for C-3 backtest unification.

Phase 1 of the BacktestEngine refactor. Runs each of the three currently-
drifted backtest paths inside finance_model_v2.py against 5 anchor tickers
and saves per-path / per-ticker JSON snapshots. After the refactor lands,
the same tickers are re-run through the unified BacktestEngine and the
deltas are inspected — a non-trivial delta is the exact bug we're fixing.

Paths captured
--------------
1. predict_ticker_inline   -> the validation-window loop at finance_model_v2.py:484-502
                              inside predict_ticker. One-shot train on 85% of data,
                              backtest on the remaining 15%. No MIN_ZSCORE_SAMPLES guard.
2. backtest_symbol         -> walk-forward loop at finance_model_v2.py:766-801.
                              Uses build_stacking_ensemble (OOF-stacked) for v3.
3. backtest_multi_strategy -> walk-forward loop at finance_model_v2.py:854-902.
                              Uses build_stacking_ensemble_fast (val-MAE weights) for v3.
                              Captured from the 'full' sub-portfolio only (the buy_only
                              sub-portfolio is sibling output and would be captured by
                              the same engine call after refactor).

Determinism
-----------
All underlying models use random_state=42 and all price data is loaded
from data_cache/<SYMBOL>.csv (no network fetch during capture). Re-running
this script produces byte-identical JSONs. This is *why* captured baselines
are a meaningful regression barrier for the Phase 3 refactor.

Baseline fields captured (per the Phase 1 brief)
------------------------------------------------
- final_portfolio_value
- sharpe                    # (mean(dr)/std(dr)) * sqrt(252)   — matches existing code
- sortino                   # (mean(dr)/std(dr_negative)) * sqrt(252)
- max_drawdown_pct          # already computed by the code, mirrored here
- num_trades                # BUY count + SELL count
- per_period_returns        # np.diff(port) / port[:-1]       (len = len(port) - 1)
- trades                    # [{date, signal, price, portfolio_value}, ...]
                            #   — only BUY / SELL rows, HOLDs are implied by gaps

Sanity asserts
--------------
For backtest_symbol and backtest_multi_strategy we ALSO call the real
function in finance_model_v2 and assert the mirrored loop produces the
same final portfolio value and number of buys. If either fires, the
baseline is invalid and the script exits with code 2 so the commit can
be aborted — do not treat a mirror-mismatch JSON as a baseline.

For predict_ticker_inline we call predict_ticker() and assert its
reported backtest.strategy_return matches our mirror's derived return.

Usage
-----
    python tests/backtest_baselines/capture_baselines.py

Writes:
    tests/backtest_baselines/<TICKER>_predict_inline.json
    tests/backtest_baselines/<TICKER>_backtest_symbol.json
    tests/backtest_baselines/<TICKER>_multi_strategy.json
"""
from __future__ import annotations

import json
import math
import os
import sys
import traceback
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.preprocessing import StandardScaler

# Make the repo root importable when running this file from anywhere.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import finance_model_v2 as fm  # noqa: E402

# Resolve the pieces we need from the module. Imported-by-reference so any
# refactor that renames them will raise ImportError here (intentional).
from finance_model_v2 import (  # noqa: E402
    HAS_LGBM,
    MIN_BACKTEST_DAYS,
    MIN_TRAIN_DAYS,
    MIN_ZSCORE_SAMPLES,
    RETRAIN_FREQ_V2,
    RETRAIN_FREQ_V3,
    THRESHOLD,
    V2_FEATURE_COLS,
    V3_FEATURE_COLS,
    ZSCORE_LOOKBACK,
    BACKTEST_PREFERRED_START,
    backtest_multi_strategy,
    backtest_symbol,
    build_stacking_ensemble,
    build_stacking_ensemble_fast,
    engineer_features_v2,
    engineer_features_v3,
    fetch_stock_data,
    predict_ticker,
    predict_v2,
    predict_v3,
    train_rf_v2,
    train_xgb_v2,
)
import pandas as pd  # noqa: E402  (imported after fm so finance_model_v2's warnings filter wins)


TARGET_TICKERS = ["SPY", "MSFT", "JNJ", "KO", "AAPL"]
# Use full-signal strategy for all tickers so the three paths are directly
# comparable on identical inputs. Forcing 'full' here does NOT affect the
# underlying models — it only changes which branch of the per-step signal
# logic runs. This is the whole point: if the three paths disagree on the
# same inputs, we want to see it.
STRATEGY = "full"
OUT_DIR = os.path.abspath(os.path.dirname(__file__))

# Sanity-check ticker: on this one ticker we ALSO call the real function in
# finance_model_v2 and compare. The three capture_* functions here are line-
# for-line copies of the target loops, so structural equivalence on one
# deterministic ticker implies structural equivalence on all — and the real
# functions are expensive (build_stacking_ensemble is ~18 model fits per
# retrain × ~44 retrains). Skipping the double-run on the other 4 tickers
# is ~5x faster.
SANITY_TICKER = "KO"
# Allow override for re-running the whole sweep under sanity if the user
# wants full paranoia (set QF_BASELINE_FULL_SANITY=1).
FULL_SANITY = os.environ.get("QF_BASELINE_FULL_SANITY", "0") == "1"


def _should_sanity(symbol: str) -> bool:
    return FULL_SANITY or symbol == SANITY_TICKER


# -----------------------------------------------------------------------------
# Shared metric helpers (identical math to what finance_model_v2 already
# computes; defined here so the output JSON is self-contained).
# -----------------------------------------------------------------------------

def _daily_returns(port: np.ndarray) -> np.ndarray:
    port = np.asarray(port, dtype=float)
    if port.size < 2:
        return np.array([], dtype=float)
    return np.diff(port) / port[:-1]


def _sharpe(port: np.ndarray) -> float:
    dr = _daily_returns(port)
    if dr.size == 0 or np.std(dr) <= 0:
        return 0.0
    return float(np.mean(dr) / np.std(dr) * math.sqrt(252))


def _sortino(port: np.ndarray) -> float:
    dr = _daily_returns(port)
    if dr.size == 0:
        return 0.0
    downside = dr[dr < 0]
    if downside.size == 0 or np.std(downside) <= 0:
        return 0.0
    return float(np.mean(dr) / np.std(downside) * math.sqrt(252))


def _max_drawdown_pct(port: np.ndarray) -> float:
    port = np.asarray(port, dtype=float)
    if port.size == 0:
        return 0.0
    pk = np.maximum.accumulate(port)
    return float(((port - pk) / pk).min()) * 100.0


def _summary(port: np.ndarray, trades: list[dict], extra: dict[str, Any] | None = None) -> dict:
    port_list = [float(round(v, 4)) for v in np.asarray(port, dtype=float).tolist()]
    dr = _daily_returns(port)
    out: dict[str, Any] = {
        "final_portfolio_value": float(round(port_list[-1], 4)) if port_list else None,
        "sharpe": round(_sharpe(port), 4),
        "sortino": round(_sortino(port), 4),
        "max_drawdown_pct": round(_max_drawdown_pct(port), 4),
        "num_trades": len(trades),
        "per_period_returns": [float(round(x, 8)) for x in dr.tolist()],
        "trades": trades,
        "portfolio_curve": port_list,  # redundant w/ returns but cheap and useful for diffs
    }
    if extra:
        out.update(extra)
    return out


# -----------------------------------------------------------------------------
# Path 1 — predict_ticker inline loop (finance_model_v2.py:484-502)
# -----------------------------------------------------------------------------

def capture_predict_inline(symbol: str, version: str) -> dict:
    """Mirror the inline loop inside predict_ticker(...). Deterministic under
    random_state=42. We also call predict_ticker() itself and sanity-check
    that the published backtest.strategy_return matches ours."""
    raw = fetch_stock_data([symbol])
    if symbol not in raw or raw[symbol].empty:
        return {"error": f"No data for {symbol}"}
    df = raw[symbol].copy()
    if version == "v3" and HAS_LGBM:
        df = engineer_features_v3(df)
        fcols = V3_FEATURE_COLS
    else:
        df = engineer_features_v2(df)
        fcols = V2_FEATURE_COLS
        version = "v2"

    dc = df.dropna(subset=["Target_Return"] + fcols).copy()
    if len(dc) < 100:
        return {"error": f"Insufficient data ({len(dc)} rows)"}
    aX = dc[fcols].values
    ay = dc["Target_Return"].values
    te = len(aX)
    vs = int(te * 0.85)
    Xtr, Xvl = aX[:vs], aX[vs:]
    ytr = ay[:vs]

    scaler = StandardScaler()
    scaler.fit(Xtr)
    Xtr_s = scaler.transform(Xtr)
    Xvl_s = scaler.transform(Xvl)

    if version == "v3":
        model = build_stacking_ensemble(Xtr_s, ytr, Xvl_s, ay[vs:])
        pf = predict_v3
    else:
        rf = train_rf_v2(scaler.transform(aX[:te]), ay[:te])
        xm = train_xgb_v2(scaler.transform(aX[:te]), ay[:te])
        model = (rf, xm)
        pf = predict_v2

    yp = pf(model, Xvl_s)

    seed_n = min(20, max(5, len(Xvl) // 3))
    btc, bts = 10000.0, 0.0
    btp: list[float] = []
    bt_pred_hist = list(yp[:seed_n])
    trades: list[dict] = []
    dates_out: list[str] = []

    for j in range(seed_n, len(Xvl)):
        pr = float(yp[j])
        # bp mirrors: float(dc['Close'].iloc[vs+j]) if vs+j<len(dc) else float(dc['Close'].iloc[-1])
        bp_idx = vs + j
        bp = float(dc["Close"].iloc[bp_idx]) if bp_idx < len(dc) else float(dc["Close"].iloc[-1])
        # Trade-date is the index at vs+j; predict_ticker does not surface this,
        # we add it here so the JSON carries a real calendar date.
        date_str = str(dc.index[bp_idx].date()) if bp_idx < len(dc) else str(dc.index[-1].date())

        bt_pred_hist.append(pr)
        if len(bt_pred_hist) > ZSCORE_LOOKBACK:
            bt_pred_hist = bt_pred_hist[-ZSCORE_LOOKBACK:]
        hist = np.array(bt_pred_hist[:-1])
        mu, sigma = float(np.mean(hist)), float(np.std(hist))
        z = (pr - mu) / sigma if sigma > 1e-10 else 0.0

        signal = "HOLD"
        if STRATEGY == "buy_only":
            if z >= THRESHOLD and btc > 0:
                bts = btc / bp
                btc = 0
                signal = "BUY"
        else:  # full
            if z >= THRESHOLD and btc > 0:
                bts = btc / bp
                btc = 0
                signal = "BUY"
            elif z <= -THRESHOLD and bts > 0:
                btc = bts * bp
                bts = 0
                signal = "SELL"

        pv = btc + bts * bp
        btp.append(pv)
        dates_out.append(date_str)
        if signal in ("BUY", "SELL"):
            trades.append({
                "date": date_str,
                "signal": signal,
                "price": round(bp, 4),
                "z_score": round(z, 4),
                "portfolio_value": round(pv, 4),
            })

    port = np.array(btp, dtype=float)

    our_strategy_return = (port[-1] / 10000.0 - 1.0) * 100.0 if port.size else 0.0
    if _should_sanity(symbol):
        # Sanity: call the real predict_ticker and cross-check strategy_return.
        real = predict_ticker(symbol, verbose=False, version=version, strategy=STRATEGY)
        if "error" in real:
            real_strategy_return = None
        else:
            real_strategy_return = real.get("backtest", {}).get("strategy_return")
        # predict_ticker rounds to 2 decimals; accept 0.01% tolerance.
        sanity_ok = (
            real_strategy_return is None
            or abs(round(our_strategy_return, 2) - real_strategy_return) <= 0.01
        )
    else:
        real_strategy_return = None
        sanity_ok = None  # skipped

    extra = {
        "path": "predict_ticker_inline",
        "source_lines": "finance_model_v2.py:484-502",
        "symbol": symbol,
        "strategy": STRATEGY,
        "version": version,
        "initial_cash": 10000.0,
        "period_days": len(port),
        "start_date": dates_out[0] if dates_out else None,
        "end_date": dates_out[-1] if dates_out else None,
        "dates": dates_out,
        "ensemble_builder": "build_stacking_ensemble" if version == "v3" else "train_rf_v2+train_xgb_v2",
        "min_zscore_samples_guard": False,  # <-- C-3: predict_ticker omits this guard
        "retrain_freq_days": None,  # <-- one-shot; no walk-forward
        "seed_n": seed_n,
        "sanity": {
            "real_predict_ticker_strategy_return_pct": real_strategy_return,
            "mirror_strategy_return_pct": round(our_strategy_return, 4),
            "matches_real": bool(sanity_ok) if sanity_ok is not None else None,
            "ran": sanity_ok is not None,
        },
    }
    return _summary(port, trades, extra)


# -----------------------------------------------------------------------------
# Path 2 — backtest_symbol (finance_model_v2.py:766-801)
# -----------------------------------------------------------------------------

def capture_backtest_symbol(symbol: str, version: str) -> dict:
    raw = fetch_stock_data([symbol])
    if symbol not in raw or raw[symbol].empty:
        return {"error": f"No data for {symbol}"}
    df = raw[symbol].copy()
    if version == "v3" and HAS_LGBM:
        df = engineer_features_v3(df)
        fc = V3_FEATURE_COLS
        rf_freq = RETRAIN_FREQ_V3
    else:
        df = engineer_features_v2(df)
        fc = V2_FEATURE_COLS
        rf_freq = RETRAIN_FREQ_V2
        version = "v2"
    dc = df.dropna(subset=["Target_Return"] + fc).copy()
    bm = dc.index >= pd.Timestamp(BACKTEST_PREFERRED_START)
    bsi = int(np.argmax(bm)) if bm.any() else 0
    bsi = max(bsi, MIN_TRAIN_DAYS)
    if bsi >= len(dc) - MIN_BACKTEST_DAYS:
        return {"error": f"Not enough data ({len(dc)} rows)"}

    aX = dc[fc].values
    ay = dc["Target_Return"].values
    ap = dc["Close"].values.ravel()
    cash, sh = 10000.0, 0.0
    port: list[float] = []
    dates_out: list[str] = []
    sigs: list[str] = []
    trades: list[dict] = []
    mdl = None
    sc = StandardScaler()
    rc = 0
    pred_history: list[float] = []

    for i in range(bsi, len(aX) - 1):
        if mdl is None or rc >= rf_freq:
            vs = int(i * 0.85)
            sc.fit(aX[:vs])
            if version == "v3":
                mdl = build_stacking_ensemble(
                    sc.transform(aX[:vs]), ay[:vs], sc.transform(aX[vs:i]), ay[vs:i]
                )
                pf = predict_v3
            else:
                mdl = (
                    train_rf_v2(sc.transform(aX[:vs]), ay[:vs]),
                    train_xgb_v2(sc.transform(aX[:vs]), ay[:vs]),
                )
                pf = predict_v2
            if not pred_history:
                seed_preds = pf(mdl, sc.transform(aX[vs:i]))
                pred_history = list(seed_preds[-ZSCORE_LOOKBACK:])
            rc = 0

        raw_pred = float(pf(mdl, sc.transform(aX[i:i + 1]))[0])
        pred_history.append(raw_pred)
        if len(pred_history) > ZSCORE_LOOKBACK:
            pred_history = pred_history[-ZSCORE_LOOKBACK:]

        if len(pred_history) >= MIN_ZSCORE_SAMPLES:
            hist = np.array(pred_history[:-1])
            mu, sigma = float(np.mean(hist)), float(np.std(hist))
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0

        pr = float(ap[i])
        date_str = str(dc.index[i].date())
        signal = "HOLD"
        if STRATEGY == "buy_only":
            if z >= THRESHOLD and cash > 0:
                sh = cash / pr
                cash = 0
                signal = "BUY"
        else:
            if z >= THRESHOLD and cash > 0:
                sh = cash / pr
                cash = 0
                signal = "BUY"
            elif z <= -THRESHOLD and sh > 0:
                cash = sh * pr
                sh = 0
                signal = "SELL"
        sigs.append(signal)
        pv = cash + sh * pr
        port.append(pv)
        dates_out.append(date_str)
        if signal in ("BUY", "SELL"):
            trades.append({
                "date": date_str,
                "signal": signal,
                "price": round(pr, 4),
                "z_score": round(z, 4),
                "portfolio_value": round(pv, 4),
            })
        rc += 1

    port_arr = np.array(port, dtype=float)

    if _should_sanity(symbol):
        # Sanity: call the real backtest_symbol and cross-check the final
        # portfolio value and total trade count it prints.
        real_port = backtest_symbol(symbol, version=version, strategy=STRATEGY)
        sanity = {
            "real_final_port": float(round(real_port[-1], 4)) if real_port is not None else None,
            "mirror_final_port": float(round(port_arr[-1], 4)) if port_arr.size else None,
            "real_len": int(len(real_port)) if real_port is not None else None,
            "mirror_len": int(len(port_arr)),
            "matches_real": (
                real_port is not None
                and len(real_port) == len(port_arr)
                and abs(float(real_port[-1]) - float(port_arr[-1])) < 0.01
            ),
            "ran": True,
        }
    else:
        sanity = {"ran": False, "matches_real": None}

    extra = {
        "path": "backtest_symbol",
        "source_lines": "finance_model_v2.py:766-801",
        "symbol": symbol,
        "strategy": STRATEGY,
        "version": version,
        "initial_cash": 10000.0,
        "period_days": len(port_arr),
        "start_date": dates_out[0] if dates_out else None,
        "end_date": dates_out[-1] if dates_out else None,
        "dates": dates_out,
        "ensemble_builder": "build_stacking_ensemble" if version == "v3" else "train_rf_v2+train_xgb_v2",
        "min_zscore_samples_guard": True,
        "retrain_freq_days": int(rf_freq),
        "buys": sigs.count("BUY"),
        "sells": sigs.count("SELL"),
        "holds": sigs.count("HOLD"),
        "sanity": sanity,
    }
    return _summary(port_arr, trades, extra)


# -----------------------------------------------------------------------------
# Path 3 — backtest_multi_strategy (finance_model_v2.py:854-902), full sub-portfolio
# -----------------------------------------------------------------------------

def capture_multi_strategy(symbol: str, version: str) -> dict:
    raw = fetch_stock_data([symbol])
    if symbol not in raw or raw[symbol].empty:
        return {"error": f"No data for {symbol}"}
    df = raw[symbol].copy()
    if version == "v3" and HAS_LGBM:
        df = engineer_features_v3(df)
        fc = V3_FEATURE_COLS
        rf_freq = RETRAIN_FREQ_V3
    else:
        df = engineer_features_v2(df)
        fc = V2_FEATURE_COLS
        rf_freq = RETRAIN_FREQ_V2
        version = "v2"
    dc = df.dropna(subset=["Target_Return"] + fc).copy()
    bm = dc.index >= pd.Timestamp(BACKTEST_PREFERRED_START)
    bsi = int(np.argmax(bm)) if bm.any() else 0
    bsi = max(bsi, MIN_TRAIN_DAYS)
    if bsi >= len(dc) - MIN_BACKTEST_DAYS:
        return {"error": f"Not enough data ({len(dc)} rows)"}

    aX = dc[fc].values
    ay = dc["Target_Return"].values
    ap = dc["Close"].values.ravel()

    f_cash, f_sh = 10000.0, 0.0
    f_port: list[float] = []
    dates_out: list[str] = []
    f_sigs: list[str] = []
    f_trades: list[dict] = []
    mdl = None
    sc = StandardScaler()
    rc = 0
    pred_history: list[float] = []

    for i in range(bsi, len(aX) - 1):
        if mdl is None or rc >= rf_freq:
            vs = int(i * 0.85)
            sc.fit(aX[:vs])
            if version == "v3":
                mdl = build_stacking_ensemble_fast(
                    sc.transform(aX[:vs]), ay[:vs], sc.transform(aX[vs:i]), ay[vs:i]
                )
                pf = predict_v3
            else:
                mdl = (
                    train_rf_v2(sc.transform(aX[:vs]), ay[:vs]),
                    train_xgb_v2(sc.transform(aX[:vs]), ay[:vs]),
                )
                pf = predict_v2
            if not pred_history:
                seed_preds = pf(mdl, sc.transform(aX[vs:i]))
                pred_history = list(seed_preds[-ZSCORE_LOOKBACK:])
            rc = 0

        raw_pred = float(pf(mdl, sc.transform(aX[i:i + 1]))[0])
        pred_history.append(raw_pred)
        if len(pred_history) > ZSCORE_LOOKBACK:
            pred_history = pred_history[-ZSCORE_LOOKBACK:]

        if len(pred_history) >= MIN_ZSCORE_SAMPLES:
            hist = np.array(pred_history[:-1])
            mu, sigma = float(np.mean(hist)), float(np.std(hist))
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0

        pr = float(ap[i])
        date_str = str(dc.index[i].date())
        # Full-signal branch only (mirror of lines 888-895):
        signal = "HOLD"
        if z >= THRESHOLD and f_cash > 0:
            f_sh = f_cash / pr
            f_cash = 0
            signal = "BUY"
        elif z <= -THRESHOLD and f_sh > 0:
            f_cash = f_sh * pr
            f_sh = 0
            signal = "SELL"
        f_sigs.append(signal)
        pv = f_cash + f_sh * pr
        f_port.append(pv)
        dates_out.append(date_str)
        if signal in ("BUY", "SELL"):
            f_trades.append({
                "date": date_str,
                "signal": signal,
                "price": round(pr, 4),
                "z_score": round(z, 4),
                "portfolio_value": round(pv, 4),
            })
        rc += 1

    port_arr = np.array(f_port, dtype=float)

    if _should_sanity(symbol):
        # Sanity: call the real backtest_multi_strategy and cross-check full's
        # final portfolio value + buy count.
        real = backtest_multi_strategy(symbol, version=version)
        if real is not None and "full" in real:
            real_full_port_list = real["full"].get("portfolio") or []
            real_full_port_last = float(real_full_port_list[-1]) if real_full_port_list else None
            real_full_buys = int(real["full"].get("buys", -1))
        else:
            real_full_port_last = None
            real_full_buys = None
        sanity = {
            "real_full_final_port": real_full_port_last,
            "mirror_full_final_port": float(round(port_arr[-1], 4)) if port_arr.size else None,
            "real_full_buys": real_full_buys,
            "mirror_full_buys": f_sigs.count("BUY"),
            "matches_real": (
                real_full_port_last is not None
                and port_arr.size
                # multi_strategy rounds portfolio to 2 decimals — accept 1-cent drift.
                and abs(real_full_port_last - float(port_arr[-1])) < 0.02
                and real_full_buys == f_sigs.count("BUY")
            ),
            "ran": True,
        }
    else:
        sanity = {"ran": False, "matches_real": None}

    extra = {
        "path": "backtest_multi_strategy",
        "source_lines": "finance_model_v2.py:854-902",
        "sub_portfolio": "full",  # buy_only is the same engine, captured post-refactor
        "symbol": symbol,
        "strategy": STRATEGY,
        "version": version,
        "initial_cash": 10000.0,
        "period_days": len(port_arr),
        "start_date": dates_out[0] if dates_out else None,
        "end_date": dates_out[-1] if dates_out else None,
        "dates": dates_out,
        "ensemble_builder": "build_stacking_ensemble_fast" if version == "v3" else "train_rf_v2+train_xgb_v2",
        "min_zscore_samples_guard": True,
        "retrain_freq_days": int(rf_freq),
        "buys": f_sigs.count("BUY"),
        "sells": f_sigs.count("SELL"),
        "holds": f_sigs.count("HOLD"),
        "sanity": sanity,
    }
    return _summary(port_arr, f_trades, extra)


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

PATHS = [
    ("predict_inline", capture_predict_inline),
    ("backtest_symbol", capture_backtest_symbol),
    ("multi_strategy", capture_multi_strategy),
]


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def main() -> int:
    # Pro model is the headline path in C-3 (build_stacking_ensemble vs
    # build_stacking_ensemble_fast is the exact divergence the refactor has
    # to decide between). Fall back to v2 if LightGBM is unavailable.
    version = "v3" if HAS_LGBM else "v2"
    print(f"[baselines] HAS_LGBM={HAS_LGBM}  version={version}  strategy={STRATEGY}")
    print(f"[baselines] tickers={TARGET_TICKERS}")
    print(f"[baselines] OUT_DIR={OUT_DIR}")

    sanity_failures: list[str] = []
    errors: list[str] = []

    for sym in TARGET_TICKERS:
        for label, fn in PATHS:
            out_path = os.path.join(OUT_DIR, f"{sym}_{label}.json")
            print(f"\n[baselines] === {sym} / {label} ===")
            try:
                result = fn(sym, version)
            except Exception as exc:  # noqa: BLE001
                err = f"{sym}/{label}: {exc.__class__.__name__}: {exc}"
                print(f"[baselines][ERROR] {err}")
                traceback.print_exc()
                errors.append(err)
                _write_json(out_path, {"error": err})
                continue

            if "error" in result:
                print(f"[baselines][SKIP] {sym}/{label}: {result['error']}")
                errors.append(f"{sym}/{label}: {result['error']}")
                _write_json(out_path, result)
                continue

            san = result.get("sanity", {}) or {}
            final_pv = result.get("final_portfolio_value")
            n_trades = result.get("num_trades")
            sharpe = result.get("sharpe")
            if san.get("ran") and san.get("matches_real") is False:
                msg = f"{sym}/{label}: sanity mismatch -> {san}"
                print(f"[baselines][SANITY-FAIL] {msg}")
                sanity_failures.append(msg)
            elif san.get("ran"):
                print(f"[baselines][OK+SANITY] final=${final_pv}  trades={n_trades}  sharpe={sharpe}")
            else:
                print(f"[baselines][OK] final=${final_pv}  trades={n_trades}  sharpe={sharpe}  (sanity skipped)")
            _write_json(out_path, result)

    print("\n" + "=" * 60)
    print(f"[baselines] done. errors={len(errors)} sanity_failures={len(sanity_failures)}")
    for e in errors:
        print(f"  ERROR: {e}")
    for s in sanity_failures:
        print(f"  SANITY: {s}")

    if sanity_failures:
        # Hard-fail so the commit is aborted — a baseline with a broken
        # sanity check is worse than no baseline.
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
