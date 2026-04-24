"""C-9: predict_ticker must flag stale-feature fallbacks via a warnings list.

When today's feature row has NaN (e.g. ROC_60d on a young IPO, a missing
Volume day), predict_ticker falls back to yesterday's complete feature row.
Prior to C-9 this happened silently; the result dict now carries
``warnings: ["stale_features_used"]`` so the UI can surface the degradation.

Strategy:
- Load real KO cached OHLCV, engineer V2 features once, cap the tail at
  ~200 rows so the internal scaler/RF/XGB fit + BacktestEngine run stay
  fast (the basic backtest test uses 400 rows and completes in seconds).
- Monkey-patch ``fetch_stock_data``/``engineer_features_v2``/``_fetch_svr``
  so predict_ticker runs against the prepared frame with no network I/O.
- For the stale case, inject a NaN into the final row's first feature
  column. ``dropna(subset=...)`` already strips the last row (Target_Return
  is shifted -1), so ``dc`` stays clean; only ``latest_row[fcols]`` goes
  NaN, which is exactly the condition predict_ticker's fallback branch
  guards.
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


_DATA_CACHE = os.path.join(REPO_ROOT, "data_cache")


def _load_engineered_ko_tail(n_rows: int = 200) -> pd.DataFrame:
    """Return a post-engineered KO frame with clean features in the tail."""
    raw = fm.fetch_stock_data(["KO"], cache_dir=_DATA_CACHE)
    df = fm.engineer_features_v2(raw["KO"].copy())
    # Keep enough post-warmup rows to satisfy predict_ticker's len(dc)>=100
    # floor after the final row gets stripped by the Target_Return dropna.
    return df.tail(n_rows).copy()


def _run_predict_ticker_with_frame(df: pd.DataFrame) -> dict:
    """Invoke predict_ticker against a pre-engineered frame, stubbing I/O.

    Keeps the monkey-patches scoped to the call — we always restore the
    originals in the finally block so other tests in the suite see the
    real module state.
    """
    orig_fetch = fm.fetch_stock_data
    orig_eng_v2 = fm.engineer_features_v2
    orig_svr = fm._fetch_svr
    try:
        fm.fetch_stock_data = lambda syms, cache_dir=None: {"KO": df.copy()}
        # Identity — df is already engineered, and predict_ticker would
        # otherwise try to re-derive Close-based columns that no longer
        # exist in the expected raw shape.
        fm.engineer_features_v2 = lambda d: d
        # Skip the yfinance .info round-trip so the test stays offline.
        fm._fetch_svr = lambda sym: (None, None, None, None, None, None)
        return fm.predict_ticker(
            "KO", cache_dir=_DATA_CACHE, verbose=False,
            version="v2", strategy="buy_only",
        )
    finally:
        fm.fetch_stock_data = orig_fetch
        fm.engineer_features_v2 = orig_eng_v2
        fm._fetch_svr = orig_svr


def test_predict_ticker_emits_stale_features_warning_on_nan() -> None:
    df = _load_engineered_ko_tail()
    # Inject NaN into the last row's first feature column. The last row has
    # NaN Target_Return already (pct_change.shift(-1)), so dropna still
    # trims it — the fallback path fires on latest_row[fcols], not on dc.
    first_fcol = fm.V2_FEATURE_COLS[0]
    col_idx = df.columns.get_loc(first_fcol)
    df.iloc[-1, col_idx] = np.nan

    res = _run_predict_ticker_with_frame(df)
    assert "error" not in res, f"unexpected error path: {res}"
    assert "warnings" in res, f"warnings key missing; got keys {list(res.keys())}"
    assert "stale_features_used" in res["warnings"], (
        f"expected 'stale_features_used' in warnings, got {res['warnings']!r}"
    )
    print(f"  [C-9] stale NaN path OK: warnings={res['warnings']}")


def test_predict_ticker_emits_empty_warnings_on_clean_data() -> None:
    df = _load_engineered_ko_tail()
    res = _run_predict_ticker_with_frame(df)
    assert "error" not in res, f"unexpected error path: {res}"
    assert "warnings" in res, f"warnings key missing; got keys {list(res.keys())}"
    assert res["warnings"] == [], (
        f"expected empty warnings on clean data, got {res['warnings']!r}"
    )
    print(f"  [C-9] clean path OK: warnings=[]")


def run_all() -> int:
    failed = 0
    for test in (
        test_predict_ticker_emits_stale_features_warning_on_nan,
        test_predict_ticker_emits_empty_warnings_on_clean_data,
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
    print("test_predict_ticker_warnings")
    rc = run_all()
    sys.exit(1 if rc else 0)
