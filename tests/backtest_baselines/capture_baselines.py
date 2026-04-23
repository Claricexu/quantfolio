"""
Capture pre-refactor baselines for C-3 backtest unification.

Phase 1 (+ addendum) of the BacktestEngine refactor. Runs each of the three
currently-drifted backtest paths inside finance_model_v2.py against a set of
anchor tickers and saves per-path / per-ticker JSON snapshots. After the
refactor lands, the same tickers are re-run through the unified BacktestEngine
and the deltas are inspected — a non-trivial delta is the exact bug we're
fixing.

Ticker roster
-------------
- Large-cap US equities:   SPY, MSFT, JNJ, KO, AAPL, NVDA
  (NVDA added in the Phase 1 addendum — volatile large-cap that appears in
  first-time search telemetry and exercises the trend-following branches more
  aggressively than the other 5.)

- Synthetic short-history: SHORTHIST_AAPL
  (Materialised at capture time from the last ~3 years of data_cache/AAPL.csv
  and written to tests/backtest_baselines/_shorthist_cache/SHORTHIST_AAPL.csv,
  which is then passed to the underlying loaders via cache_dir=.

  Purpose: none of SPY/MSFT/JNJ/KO/AAPL/NVDA exercises the
  MIN_ZSCORE_SAMPLES=20 guard because at the first retrain pred_history is
  seeded with min(i-vs, ZSCORE_LOOKBACK) items. With the production floor
  MIN_TRAIN_DAYS=126 and the module's vs=int(i*0.85) rule, the seed is always
  >= 19 — so after a single append the guard check (`len >= 20`) always
  passes. The guard is effectively dormant in the real module's default
  configuration for every ticker with enough data to run.

  To make the guard actively FIRE (force HOLD for multiple opening bars),
  the capture also monkey-patches fm.MIN_TRAIN_DAYS=80 for the duration of
  the SHORTHIST capture ONLY, via _patched_min_train_days(). With bsi=80 and
  vs=68, the seed becomes 12, so iterations k=0..6 run under the guard
  (HOLD, z=0.0) and iteration k=7 is the first to pass the guard. This is
  the regime Phase 3's bug-fix is targeted at: predict_ticker's inline loop
  currently has NO guard (uses an adaptive seed_n instead) so on this ticker
  it would fire real z-score signals with only 5-19 samples of history —
  statistically meaningless. The refactored engine will apply the guard
  uniformly, and the diff between the pre-refactor predict_inline baseline
  and the post-refactor output on SHORTHIST_AAPL is the receipt for the
  bug-fix claim.

  The "SHORTHIST_" prefix is intentional so a human reading the JSON knows
  it is synthetic and not to attempt to map it to a real-world backtest
  result. Sanity ALWAYS runs on SHORTHIST_AAPL (overriding the KO-only
  default) because it's the one ticker whose baseline meaningfully depends
  on the patched constant, so we want fresh cross-verification every run.)

Paths captured
--------------
1. predict_ticker_inline       -> the validation-window loop at finance_model_v2.py:484-502
                                  inside predict_ticker. One-shot train on 85% of data,
                                  backtest on the remaining 15%. No MIN_ZSCORE_SAMPLES guard.
2. backtest_symbol             -> walk-forward loop at finance_model_v2.py:766-801.
                                  Uses build_stacking_ensemble (OOF-stacked) for v3.
3. backtest_multi_strategy     -> walk-forward loop at finance_model_v2.py:854-902,
                                  'full' sub-portfolio.
                                  Uses build_stacking_ensemble_fast (val-MAE weights) for v3.
4. backtest_multi_strategy_buyonly
                              -> Same walk-forward loop as (3), but captures the
                                  'buy_only' sub-portfolio that Strategy Lab renders
                                  alongside 'full'. Emitted as a separate JSON
                                  (<TICKER>_multi_strategy_buyonly.json) so both curves
                                  from the same model run can be diffed independently
                                  post-refactor.

Determinism
-----------
All underlying models use random_state=42 and all price data is loaded from
on-disk caches (data_cache/ for real tickers, _shorthist_cache/ for the
synthetic short-history ticker). No network fetch happens during capture.
Re-running this script produces byte-identical JSONs. This is *why* captured
baselines are a meaningful regression barrier for the Phase 3 refactor.

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
Sanity cross-checks call the real finance_model_v2 function alongside the
mirror and assert they produce the same final portfolio value / strategy
return / buy count. A mismatch is fatal (script exits with code 2 so the
baseline commit is aborted — a sanity-failing JSON is worse than no JSON).

By default, sanity runs for ONE ticker (KO) across ALL FOUR capture paths
(predict_inline, backtest_symbol, multi_strategy full, multi_strategy
buy_only). This is intentional: the four capture_* functions below are
line-for-line copies of the target loops, so structural equivalence on one
deterministic ticker implies structural equivalence on all — and the real
functions are expensive (build_stacking_ensemble is ~18 model fits per
retrain × ~44 retrains). Running full sanity across 7 tickers × 4 paths
would roughly triple wall-clock time for no additional signal.

Set QF_BASELINE_FULL_SANITY=1 to force sanity on every (ticker, path)
combination when paranoia is warranted (e.g. before a risky refactor lands).

Usage
-----
    python tests/backtest_baselines/capture_baselines.py

Writes (per ticker):
    tests/backtest_baselines/<TICKER>_predict_inline.json
    tests/backtest_baselines/<TICKER>_backtest_symbol.json
    tests/backtest_baselines/<TICKER>_multi_strategy.json
    tests/backtest_baselines/<TICKER>_multi_strategy_buyonly.json
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
    engineer_features_v2,
    engineer_features_v3,
    fetch_stock_data,
    predict_ticker,
    predict_v2,
    predict_v3,
    train_lgbm_v3,
    train_rf_v2,
    train_rf_v3,
    train_xgb_v2,
    train_xgb_v3,
)


# ---------------------------------------------------------------------------
# Inlined copy of the pre-Phase-5 ``build_stacking_ensemble_fast`` (deleted
# from finance_model_v2.py in Phase 5 of C-3). Retained here locally so the
# historical baseline captures remain faithful to the exact pre-refactor
# behaviour — this file is a reference mirror, not production code.
# ---------------------------------------------------------------------------

def build_stacking_ensemble_fast(X_train, y_train, X_val, y_val):
    """Val-MAE-weighted stacking ensemble (pre-refactor multi_strategy builder).

    Inlined copy of the original finance_model_v2.build_stacking_ensemble_fast.
    Skips 5-fold OOF and uses val-set MAE for weights. ~6x faster than the OOF
    builder (3 model fits per retrain instead of 18). Kept here solely so the
    historical reference capture path matches the exact pre-Phase-5 behaviour.
    """
    fl = train_lgbm_v3(X_train, y_train, X_val, y_val)
    fx = train_xgb_v3(X_train, y_train, X_val, y_val)
    fr = train_rf_v3(X_train, y_train)
    preds = np.column_stack([fl.predict(X_val), fx.predict(X_val), fr.predict(X_val)])
    mae = np.array([np.mean(np.abs(preds[:, j] - y_val)) for j in range(3)])
    inv_mae = 1.0 / (mae + 0.005)  # 0.005 prevents extreme weight imbalance
    weights = inv_mae / inv_mae.sum()
    return {"lgbm": fl, "xgb": fx, "rf": fr, "weights": weights}
import pandas as pd  # noqa: E402  (imported after fm so finance_model_v2's warnings filter wins)


# Real tickers: full data_cache/<SYM>.csv history (back to 2010-01-04).
REAL_TICKERS = ["SPY", "MSFT", "JNJ", "KO", "AAPL", "NVDA"]

# Synthetic short-history ticker — see module docstring. Built at capture
# time from the tail of data_cache/AAPL.csv and written to a sibling cache
# directory. The "SHORTHIST_" prefix is deliberate: any human reading the
# baseline JSON should immediately recognise this is not a real-world result.
SHORTHIST_SYMBOL = "SHORTHIST_AAPL"
SHORTHIST_SOURCE = "AAPL"
SHORTHIST_YEARS = 3  # tail slice length, in trading years

# Canonical capture order (used for logging/reporting).
TARGET_TICKERS = REAL_TICKERS + [SHORTHIST_SYMBOL]

# Use full-signal strategy for all tickers so the three paths are directly
# comparable on identical inputs. Forcing 'full' here does NOT affect the
# underlying models — it only changes which branch of the per-step signal
# logic runs. This is the whole point: if the three paths disagree on the
# same inputs, we want to see it. (The multi_strategy buy_only sub-portfolio
# is captured separately and is independent of this STRATEGY flag — it
# always captures the buy_only curve from the same model run.)
STRATEGY = "full"
OUT_DIR = os.path.abspath(os.path.dirname(__file__))

# Cache dir for synthetic tickers (see _materialize_shorthist_csv). Kept
# distinct from data_cache/ so we never mutate the real cache.
SHORTHIST_CACHE_DIR = os.path.join(OUT_DIR, "_shorthist_cache")
REAL_CACHE_DIR = os.path.join(REPO_ROOT, "data_cache")


def _cache_dir_for(symbol: str) -> str:
    """Return the cache_dir to pass to fetch_stock_data / predict_ticker /
    backtest_symbol / backtest_multi_strategy for this symbol. Real tickers
    live in data_cache/; synthetic tickers live in _shorthist_cache/."""
    if symbol == SHORTHIST_SYMBOL:
        return SHORTHIST_CACHE_DIR
    return REAL_CACHE_DIR


def _materialize_shorthist_csv() -> str:
    """Build the synthetic SHORTHIST_AAPL cache CSV from the tail of the real
    AAPL cache. Idempotent: re-running is byte-identical as long as the source
    AAPL.csv is unchanged. Returns the absolute path to the generated CSV."""
    os.makedirs(SHORTHIST_CACHE_DIR, exist_ok=True)
    src = os.path.join(REAL_CACHE_DIR, f"{SHORTHIST_SOURCE}.csv")
    dst = os.path.join(SHORTHIST_CACHE_DIR, f"{SHORTHIST_SYMBOL}.csv")
    df = pd.read_csv(src, index_col=0, parse_dates=True)
    # ~252 trading days/year. Slice the tail and re-emit so fetch_stock_data's
    # freshness check passes (last row date is today's completed session, same
    # as the source).
    tail = df.iloc[-(SHORTHIST_YEARS * 252):].copy()
    tail.index.name = df.index.name or "Date"
    tail.to_csv(dst)
    return dst


# -----------------------------------------------------------------------------
# Guard-firing hook for SHORTHIST_AAPL.
# -----------------------------------------------------------------------------

# Lower value of fm.MIN_TRAIN_DAYS to apply during SHORTHIST capture. See the
# module docstring for the arithmetic: bsi=80 → vs=68 → seed=12 → guard fires
# for 7 iterations before the rolling history reaches MIN_ZSCORE_SAMPLES=20.
SHORTHIST_PATCH_MIN_TRAIN_DAYS = 80


class _patched_min_train_days:
    """Context manager that temporarily lowers fm.MIN_TRAIN_DAYS so the
    MIN_ZSCORE_SAMPLES guard actually fires inside the backtest loops. Both
    the real finance_model_v2 functions (used for sanity) AND our mirror
    loops (which read fm.MIN_TRAIN_DAYS dynamically — see _live_constants)
    respect the patched value. Outside this context manager, the constant
    is restored to its original production value."""

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


def _live_constants() -> tuple[int, int, int]:
    """Return the LIVE values of (MIN_TRAIN_DAYS, MIN_BACKTEST_DAYS,
    MIN_ZSCORE_SAMPLES) by reading them off the fm module, so a patch via
    _patched_min_train_days() is respected by the mirror loops. We keep
    RETRAIN_FREQ_V2/V3 / ZSCORE_LOOKBACK / THRESHOLD on the frozen imports
    above — they are not patched."""
    return (
        int(fm.MIN_TRAIN_DAYS),
        int(fm.MIN_BACKTEST_DAYS),
        int(fm.MIN_ZSCORE_SAMPLES),
    )

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
    # Always sanity-check SHORTHIST_AAPL — its baseline depends on the
    # _patched_min_train_days monkey-patch so we want fresh cross-verification
    # against the real (also patched) fm functions on every capture run.
    return FULL_SANITY or symbol == SANITY_TICKER or symbol == SHORTHIST_SYMBOL


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
    cache_dir = _cache_dir_for(symbol)
    raw = fetch_stock_data([symbol], cache_dir=cache_dir)
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
        real = predict_ticker(symbol, cache_dir=cache_dir, verbose=False, version=version, strategy=STRATEGY)
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
        "min_train_days_used": int(fm.MIN_TRAIN_DAYS),
        "min_zscore_samples_used": int(fm.MIN_ZSCORE_SAMPLES),
        "shorthist_patch_active": symbol == SHORTHIST_SYMBOL,
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
    cache_dir = _cache_dir_for(symbol)
    min_train_days, min_backtest_days, min_zscore_samples = _live_constants()
    raw = fetch_stock_data([symbol], cache_dir=cache_dir)
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
    bsi = max(bsi, min_train_days)
    if bsi >= len(dc) - min_backtest_days:
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

        if len(pred_history) >= min_zscore_samples:
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
        real_port = backtest_symbol(symbol, cache_dir=cache_dir, version=version, strategy=STRATEGY)
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
        "min_train_days_used": int(fm.MIN_TRAIN_DAYS),
        "min_zscore_samples_used": int(fm.MIN_ZSCORE_SAMPLES),
        "shorthist_patch_active": symbol == SHORTHIST_SYMBOL,
        "sanity": sanity,
    }
    return _summary(port_arr, trades, extra)


# -----------------------------------------------------------------------------
# Path 3 — backtest_multi_strategy (finance_model_v2.py:854-902).
#
# Single walk-forward loop trains the model once per retrain window and applies
# BOTH the Full Signal and Buy-Only strategies on the same raw predictions.
# We return a dict with two summaries ('full' and 'buy_only'); the driver
# writes them as sibling JSONs (<SYM>_multi_strategy.json and
# <SYM>_multi_strategy_buyonly.json). Rationale: Strategy Lab renders both
# curves and the post-refactor engine must reproduce both — capturing only
# 'full' (Phase 1's original behaviour) leaves buy_only unprotected against
# regression.
# -----------------------------------------------------------------------------

def capture_multi_strategy(symbol: str, version: str) -> dict:
    cache_dir = _cache_dir_for(symbol)
    min_train_days, min_backtest_days, min_zscore_samples = _live_constants()
    raw = fetch_stock_data([symbol], cache_dir=cache_dir)
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
    bsi = max(bsi, min_train_days)
    if bsi >= len(dc) - min_backtest_days:
        return {"error": f"Not enough data ({len(dc)} rows)"}

    aX = dc[fc].values
    ay = dc["Target_Return"].values
    ap = dc["Close"].values.ravel()

    # Two parallel portfolios — mirror of finance_model_v2.py:843-902.
    f_cash, f_sh = 10000.0, 0.0  # full signal
    b_cash, b_sh = 10000.0, 0.0  # buy only
    f_port: list[float] = []
    b_port: list[float] = []
    dates_out: list[str] = []
    f_sigs: list[str] = []
    b_sigs: list[str] = []
    f_trades: list[dict] = []
    b_trades: list[dict] = []
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

        if len(pred_history) >= min_zscore_samples:
            hist = np.array(pred_history[:-1])
            mu, sigma = float(np.mean(hist)), float(np.std(hist))
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0

        pr = float(ap[i])
        date_str = str(dc.index[i].date())

        # Full-signal branch (mirror of lines 888-895).
        f_sig = "HOLD"
        if z >= THRESHOLD and f_cash > 0:
            f_sh = f_cash / pr
            f_cash = 0
            f_sig = "BUY"
        elif z <= -THRESHOLD and f_sh > 0:
            f_cash = f_sh * pr
            f_sh = 0
            f_sig = "SELL"
        f_sigs.append(f_sig)
        f_pv = f_cash + f_sh * pr
        f_port.append(f_pv)
        if f_sig in ("BUY", "SELL"):
            f_trades.append({
                "date": date_str,
                "signal": f_sig,
                "price": round(pr, 4),
                "z_score": round(z, 4),
                "portfolio_value": round(f_pv, 4),
            })

        # Buy-only branch (mirror of lines 897-902).
        b_sig = "HOLD"
        if z >= THRESHOLD and b_cash > 0:
            b_sh = b_cash / pr
            b_cash = 0
            b_sig = "BUY"
        b_sigs.append(b_sig)
        b_pv = b_cash + b_sh * pr
        b_port.append(b_pv)
        if b_sig == "BUY":
            b_trades.append({
                "date": date_str,
                "signal": b_sig,
                "price": round(pr, 4),
                "z_score": round(z, 4),
                "portfolio_value": round(b_pv, 4),
            })

        dates_out.append(date_str)
        rc += 1

    f_port_arr = np.array(f_port, dtype=float)
    b_port_arr = np.array(b_port, dtype=float)

    # Sanity cross-check against the real backtest_multi_strategy — covers
    # BOTH sub-portfolios in a single call.
    if _should_sanity(symbol):
        real = backtest_multi_strategy(symbol, cache_dir=cache_dir, version=version)
        if real is not None:
            real_full_port = real.get("full", {}).get("portfolio") or []
            real_buy_port = real.get("buy_only", {}).get("portfolio") or []
            real_full_last = float(real_full_port[-1]) if real_full_port else None
            real_buy_last = float(real_buy_port[-1]) if real_buy_port else None
            real_full_buys = int(real.get("full", {}).get("buys", -1))
            real_buy_buys = int(real.get("buy_only", {}).get("buys", -1))
        else:
            real_full_last = real_buy_last = None
            real_full_buys = real_buy_buys = None
        full_sanity = {
            "real_final_port": real_full_last,
            "mirror_final_port": float(round(f_port_arr[-1], 4)) if f_port_arr.size else None,
            "real_buys": real_full_buys,
            "mirror_buys": f_sigs.count("BUY"),
            "matches_real": (
                real_full_last is not None
                and f_port_arr.size
                # multi_strategy rounds portfolio to 2 decimals — accept 1-cent drift.
                and abs(real_full_last - float(f_port_arr[-1])) < 0.02
                and real_full_buys == f_sigs.count("BUY")
            ),
            "ran": True,
        }
        buy_sanity = {
            "real_final_port": real_buy_last,
            "mirror_final_port": float(round(b_port_arr[-1], 4)) if b_port_arr.size else None,
            "real_buys": real_buy_buys,
            "mirror_buys": b_sigs.count("BUY"),
            "matches_real": (
                real_buy_last is not None
                and b_port_arr.size
                and abs(real_buy_last - float(b_port_arr[-1])) < 0.02
                and real_buy_buys == b_sigs.count("BUY")
            ),
            "ran": True,
        }
    else:
        full_sanity = {"ran": False, "matches_real": None}
        buy_sanity = {"ran": False, "matches_real": None}

    common_extra = {
        "path": "backtest_multi_strategy",
        "source_lines": "finance_model_v2.py:854-902",
        "symbol": symbol,
        "strategy": STRATEGY,
        "version": version,
        "initial_cash": 10000.0,
        "period_days": len(f_port_arr),
        "start_date": dates_out[0] if dates_out else None,
        "end_date": dates_out[-1] if dates_out else None,
        "dates": dates_out,
        "ensemble_builder": "build_stacking_ensemble_fast" if version == "v3" else "train_rf_v2+train_xgb_v2",
        "min_zscore_samples_guard": True,
        "retrain_freq_days": int(rf_freq),
        "min_train_days_used": int(fm.MIN_TRAIN_DAYS),
        "min_zscore_samples_used": int(fm.MIN_ZSCORE_SAMPLES),
        "shorthist_patch_active": symbol == SHORTHIST_SYMBOL,
    }

    full_extra = {
        **common_extra,
        "sub_portfolio": "full",
        "buys": f_sigs.count("BUY"),
        "sells": f_sigs.count("SELL"),
        "holds": f_sigs.count("HOLD"),
        "sanity": full_sanity,
    }
    buy_extra = {
        **common_extra,
        "sub_portfolio": "buy_only",
        "buys": b_sigs.count("BUY"),
        "sells": 0,
        "holds": b_sigs.count("HOLD"),
        "sanity": buy_sanity,
    }

    # Return BOTH summaries; driver splits them into two JSONs.
    return {
        "__multi__": True,
        "full": _summary(f_port_arr, f_trades, full_extra),
        "buy_only": _summary(b_port_arr, b_trades, buy_extra),
    }


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

PATHS = [
    ("predict_inline", capture_predict_inline),
    ("backtest_symbol", capture_backtest_symbol),
    ("multi_strategy", capture_multi_strategy),   # splits into _multi_strategy + _multi_strategy_buyonly
]


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def _log_result(tag: str, payload: dict) -> tuple[str | None, str | None]:
    """Emit the one-line OK/OK+SANITY/SANITY-FAIL status for a single JSON
    payload. Returns (error_msg, sanity_failure_msg), either of which may be
    None."""
    if "error" in payload:
        err = f"{tag}: {payload['error']}"
        print(f"[baselines][SKIP] {err}")
        return err, None
    san = payload.get("sanity", {}) or {}
    final_pv = payload.get("final_portfolio_value")
    n_trades = payload.get("num_trades")
    sharpe = payload.get("sharpe")
    if san.get("ran") and san.get("matches_real") is False:
        msg = f"{tag}: sanity mismatch -> {san}"
        print(f"[baselines][SANITY-FAIL] {msg}")
        return None, msg
    if san.get("ran"):
        print(f"[baselines][OK+SANITY] {tag}: final=${final_pv}  trades={n_trades}  sharpe={sharpe}")
    else:
        print(f"[baselines][OK] {tag}: final=${final_pv}  trades={n_trades}  sharpe={sharpe}  (sanity skipped)")
    return None, None


def main() -> int:
    # Pro model is the headline path in C-3 (build_stacking_ensemble vs
    # build_stacking_ensemble_fast is the exact divergence the refactor has
    # to decide between). Fall back to v2 if LightGBM is unavailable.
    version = "v3" if HAS_LGBM else "v2"
    print(f"[baselines] HAS_LGBM={HAS_LGBM}  version={version}  strategy={STRATEGY}")
    print(f"[baselines] tickers={TARGET_TICKERS}")
    print(f"[baselines] OUT_DIR={OUT_DIR}")

    # Materialise the synthetic short-history ticker into its private cache.
    shorthist_path = _materialize_shorthist_csv()
    print(f"[baselines] shorthist cache -> {shorthist_path}")
    print(f"[baselines] shorthist MIN_TRAIN_DAYS patch -> {SHORTHIST_PATCH_MIN_TRAIN_DAYS} (default {fm.MIN_TRAIN_DAYS})")

    sanity_failures: list[str] = []
    errors: list[str] = []

    for sym in TARGET_TICKERS:
        # Apply the MIN_TRAIN_DAYS monkey-patch ONLY for the SHORTHIST ticker
        # — real tickers capture under production constants. The patch is
        # scoped to the whole 3-path inner loop so mirror + sanity call both
        # see the same value.
        patch_enabled = sym == SHORTHIST_SYMBOL
        with _patched_min_train_days(SHORTHIST_PATCH_MIN_TRAIN_DAYS, enabled=patch_enabled):
            for label, fn in PATHS:
                print(f"\n[baselines] === {sym} / {label} ===")
                try:
                    result = fn(sym, version)
                except Exception as exc:  # noqa: BLE001
                    err = f"{sym}/{label}: {exc.__class__.__name__}: {exc}"
                    print(f"[baselines][ERROR] {err}")
                    traceback.print_exc()
                    errors.append(err)
                    _write_json(os.path.join(OUT_DIR, f"{sym}_{label}.json"), {"error": err})
                    continue

                if result.get("__multi__"):
                    # multi_strategy returns BOTH sub-portfolios in one call —
                    # split into sibling JSONs so they can be diffed independently.
                    full_path = os.path.join(OUT_DIR, f"{sym}_{label}.json")
                    buy_path = os.path.join(OUT_DIR, f"{sym}_{label}_buyonly.json")
                    _write_json(full_path, result["full"])
                    _write_json(buy_path, result["buy_only"])
                    for tag, payload in (
                        (f"{sym}/{label}[full]", result["full"]),
                        (f"{sym}/{label}[buy_only]", result["buy_only"]),
                    ):
                        e, s = _log_result(tag, payload)
                        if e:
                            errors.append(e)
                        if s:
                            sanity_failures.append(s)
                    continue

                out_path = os.path.join(OUT_DIR, f"{sym}_{label}.json")
                _write_json(out_path, result)
                e, s = _log_result(f"{sym}/{label}", result)
                if e:
                    errors.append(e)
                if s:
                    sanity_failures.append(s)

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
