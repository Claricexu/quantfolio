"""Unit tests for ``fundamental_screener.apply_peer_medians`` — Round 7d.

Plain-assert style, matching the rest of ``tests/unit/``. The function under
test is pure over the ``scored`` list-of-dicts (industry_group bucketing,
median over non-null values, min_peers threshold), so no DB / network is
needed. The CSV roundtrip test uses a tmp file + verdict_provider's reader
to confirm float coercion lands on the new columns.
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fundamental_screener import (  # noqa: E402
    PEER_MEDIAN_METRICS,
    apply_peer_medians,
    write_screener_csv,
    CSV_OUT_FIELDS,
)
import verdict_provider  # noqa: E402


# ─── Helpers ────────────────────────────────────────────────────────────────

def _row(symbol, industry_group, **metrics):
    """Build a minimal scored-row dict.

    Defaults: every PEER_MEDIAN_METRIC is set to a placeholder value unless
    the caller explicitly passes None (or omits and we fill with the index-
    keyed default). Tests override the few metrics they care about.
    """
    base = {
        'symbol': symbol,
        'industry_group': industry_group,
        # CSV_OUT_FIELDS staples — set so write_screener_csv doesn't KeyError
        'name': symbol,
        'sector': 'Technology',
        'verdict': 'WATCH',
        'good_firm_score': 50,
        'archetype': 'GROWTH',
        'tests_passed': 4,
        'tests_known': 5,
    }
    for m in PEER_MEDIAN_METRICS:
        base[m] = metrics.get(m, None)
    return base


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    except Exception as exc:  # surface unexpected errors as failures
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


# ─── 1. Basic aggregation ────────────────────────────────────────────────────

def test_peer_median_basic_aggregation():
    """6 rows in the same industry_group with simple integer-valued metrics
    produce per-metric medians equal to the median of those 6 values, and
    peer_count == 6 on every row."""
    # Use revenue_yoy_growth as the canary; values 0.10..0.60. Median = 0.35.
    values = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60]
    rows = [
        _row(f"T{i}", "Semiconductors", revenue_yoy_growth=v)
        for i, v in enumerate(values)
    ]
    apply_peer_medians(rows)
    for r in rows:
        assert r['peer_count'] == 6, f"peer_count={r['peer_count']}"
        assert r['peer_median_revenue_yoy_growth'] == 0.35, (
            f"got {r['peer_median_revenue_yoy_growth']}"
        )
    # Other metrics had None values for all 6 rows — below min_peers, None.
    for r in rows:
        for m in PEER_MEDIAN_METRICS:
            if m == 'revenue_yoy_growth':
                continue
            assert r[f'peer_median_{m}'] is None, (
                f"{m} should be None, got {r[f'peer_median_{m}']}"
            )


# ─── 2. Below min_peers threshold ────────────────────────────────────────────

def test_peer_median_below_min_threshold_returns_none():
    """4 rows in a bucket (< min_peers=5) → all 8 peer_median_* are None,
    but peer_count is still 4 (size of bucket)."""
    rows = [
        _row(f"T{i}", "Banks", revenue_yoy_growth=0.05 * i, roic_ttm=0.10 * i)
        for i in range(1, 5)  # 4 rows
    ]
    apply_peer_medians(rows)
    for r in rows:
        assert r['peer_count'] == 4
        for m in PEER_MEDIAN_METRICS:
            assert r[f'peer_median_{m}'] is None, (
                f"{m} should be None at n=4, got {r[f'peer_median_{m}']}"
            )


# ─── 3. Excludes Nones from count ───────────────────────────────────────────

def test_peer_median_excludes_nones_from_count():
    """6 rows; only 3 have non-null SVR → SVR peer-median is None (below
    min_peers=5 of non-null values). Other metrics with ≥5 non-nulls compute
    correctly."""
    # All 6 rows have gross_margin_ttm; 3 have SVR.
    gm_values = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65]  # median = 0.525
    svr_values = [2.0, 4.0, 6.0, None, None, None]    # only 3 non-null
    rows = []
    for i in range(6):
        rows.append(_row(
            f"T{i}", "Pharmaceuticals",
            gross_margin_ttm=gm_values[i],
            svr=svr_values[i],
        ))
    apply_peer_medians(rows)
    for r in rows:
        assert r['peer_count'] == 6
        assert r['peer_median_gross_margin_ttm'] == 0.525, (
            f"got {r['peer_median_gross_margin_ttm']}"
        )
        assert r['peer_median_svr'] is None, (
            f"SVR median should be None (3 non-nulls < min_peers=5), "
            f"got {r['peer_median_svr']}"
        )


# ─── 4. Missing industry_group (ETFs / classifier gaps) ──────────────────────

def test_peer_median_handles_missing_industry_group():
    """5 rows with no industry_group (empty string and None mixed) → no
    peer_median_* or peer_count columns set on those rows. Function does
    NOT raise."""
    rows = [
        _row("ETF1", None, revenue_yoy_growth=0.10),
        _row("ETF2", None, revenue_yoy_growth=0.20),
        _row("ETF3", "", revenue_yoy_growth=0.30),
        _row("ETF4", "", revenue_yoy_growth=0.40),
        _row("ETF5", None, revenue_yoy_growth=0.50),
    ]
    apply_peer_medians(rows)
    for r in rows:
        assert 'peer_count' not in r, (
            f"{r['symbol']} got peer_count={r.get('peer_count')}"
        )
        for m in PEER_MEDIAN_METRICS:
            assert f'peer_median_{m}' not in r, (
                f"{r['symbol']} got peer_median_{m}="
                f"{r.get(f'peer_median_{m}')}"
            )


# ─── 5. Per-bucket isolation ─────────────────────────────────────────────────

def test_peer_median_isolates_per_industry_group():
    """Two industry groups, very different roic distributions — Software
    bucket median must not leak into Pharmaceuticals bucket median."""
    sw_rocs = [0.10, 0.15, 0.20, 0.25, 0.30]      # median 0.20
    px_rocs = [0.40, 0.45, 0.50, 0.55, 0.60]      # median 0.50
    rows = []
    for i, v in enumerate(sw_rocs):
        rows.append(_row(f"SW{i}", "Software", roic_ttm=v))
    for i, v in enumerate(px_rocs):
        rows.append(_row(f"PX{i}", "Pharmaceuticals", roic_ttm=v))
    apply_peer_medians(rows)

    sw = [r for r in rows if r['industry_group'] == 'Software']
    px = [r for r in rows if r['industry_group'] == 'Pharmaceuticals']
    assert len(sw) == 5 and len(px) == 5
    for r in sw:
        assert r['peer_count'] == 5
        assert r['peer_median_roic_ttm'] == 0.20, (
            f"Software got {r['peer_median_roic_ttm']}"
        )
    for r in px:
        assert r['peer_count'] == 5
        assert r['peer_median_roic_ttm'] == 0.50, (
            f"Pharmaceuticals got {r['peer_median_roic_ttm']}"
        )


# ─── 6. CSV roundtrip ────────────────────────────────────────────────────────

def test_peer_median_csv_roundtrip():
    """write_screener_csv emits the new columns; verdict_provider's CSV
    reader coerces them back to floats (and peer_count to int). Confirms the
    schema wiring is end-to-end consistent — the bug class this guards
    against is "added column to write path but forgot to register in
    _FLOAT_COLS / _INT_COLS"."""
    rows = [
        _row(f"T{i}", "Semiconductors",
             revenue_yoy_growth=0.10 * (i + 1),
             gross_margin_ttm=0.40 + 0.02 * i,
             roic_ttm=0.15 + 0.01 * i,
             svr=3.0 + i,
             rule_40_score=30.0 + 5 * i,
             operating_margin_ttm=0.20 + 0.01 * i,
             fcf_margin_ttm=0.15 + 0.01 * i,
             revenue_3y_cagr=0.18 + 0.005 * i)
        for i in range(6)
    ]
    apply_peer_medians(rows)
    # tests_json / dealbreakers_json fields are populated by write_screener_csv
    # only if `tests` / `dealbreakers` are set on the row. We don't set them,
    # so they'll write as empty strings — fine for this test.

    with tempfile.TemporaryDirectory() as td:
        csv_path = os.path.join(td, "screener_results.csv")
        write_screener_csv(rows, csv_path)

        # Sanity: header carries the new columns.
        with open(csv_path, "r", encoding="utf-8") as f:
            header = next(csv.reader(f))
        for col in (
            "peer_median_revenue_yoy_growth",
            "peer_median_gross_margin_ttm",
            "peer_median_roic_ttm",
            "peer_median_svr",
            "peer_median_rule_40_score",
            "peer_median_operating_margin_ttm",
            "peer_median_fcf_margin_ttm",
            "peer_median_revenue_3y_cagr",
            "peer_count",
        ):
            assert col in header, f"missing column {col} in header"
        assert "svr_vs_sector_median" not in header, (
            "svr_vs_sector_median should be gone from CSV header"
        )

        # Round-trip via verdict_provider's reader. Use force_reload to bypass
        # the module-level mtime cache (other tests / earlier runs may have
        # populated it).
        index = verdict_provider.load_screener_index(
            path=csv_path, force_reload=True
        )

    assert "T0" in index, f"T0 missing from index keys={list(index)[:3]}"
    row0 = index["T0"]

    # Float coercion must land on every peer_median_* column.
    for m in PEER_MEDIAN_METRICS:
        col = f"peer_median_{m}"
        v = row0.get(col)
        assert v is not None, f"{col} is None after roundtrip"
        assert isinstance(v, float), (
            f"{col} should be float, got {type(v).__name__}={v}"
        )

    # peer_count coerces to int (registered in _INT_COLS).
    pc = row0.get("peer_count")
    assert pc == 6, f"peer_count={pc}"
    assert isinstance(pc, int), (
        f"peer_count should be int, got {type(pc).__name__}={pc}"
    )


# ─── Suite entrypoint (run_all.py contract) ──────────────────────────────────

TESTS = [
    ("test_peer_median_basic_aggregation",
     test_peer_median_basic_aggregation),
    ("test_peer_median_below_min_threshold_returns_none",
     test_peer_median_below_min_threshold_returns_none),
    ("test_peer_median_excludes_nones_from_count",
     test_peer_median_excludes_nones_from_count),
    ("test_peer_median_handles_missing_industry_group",
     test_peer_median_handles_missing_industry_group),
    ("test_peer_median_isolates_per_industry_group",
     test_peer_median_isolates_per_industry_group),
    ("test_peer_median_csv_roundtrip",
     test_peer_median_csv_roundtrip),
]


def run_all() -> int:
    fails = 0
    for name, fn in TESTS:
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(0 if run_all() == 0 else 1)
