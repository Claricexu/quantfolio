"""Phase 1.8 Step 2 — 2-archetype split diagnostic (pre-tuning).

Read-only characterization of how the 1,430 scored tickers fall into MATURE vs
GROWTH under three candidate classifiers, at three candidate thresholds each.
Also shows where ~25 anchor tickers land (validation that the classifier makes
sense) and per-group histograms for the metrics we'd use in per-group rubrics.

No code changes, no CSV writes. ~5 sec runtime.

Usage:
    python diag_archetype_split.py

After running this, we'll have:
  (1) A defensible choice of classifier + threshold
  (2) Empirical distribution shape for each group's key metrics
  (3) Ground to draft per-group tests in fundamental_screener.py Phase 1.9
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

CSV_PATH = Path(__file__).parent / 'screener_results.csv'

# ─── Anchor tickers — validation that the classifier does what we expect ─────
# If any of these land in the "wrong" group, the classifier is broken.
ANCHOR_MATURE = [
    'KO', 'JNJ', 'CVX', 'PG', 'WMT', 'XOM', 'VZ', 'HD', 'MMM',
    'DIS', 'F', 'GM', 'DUK', 'NEE', 'SO', 'MCD', 'PEP', 'T', 'IBM',
]
ANCHOR_GROWTH = [
    'NOW', 'CRWD', 'SNOW', 'NVDA', 'CRM', 'DDOG', 'ZS', 'PANW',
    'PLTR', 'NFLX', 'TSLA', 'ANET',
]
ANCHOR_BORDERLINE = [
    'MSFT', 'META', 'AAPL', 'AMZN', 'GOOGL', 'LLY',  # big, still growing
]
ALL_ANCHORS = ANCHOR_MATURE + ANCHOR_GROWTH + ANCHOR_BORDERLINE


# ─── CSV helpers ──────────────────────────────────────────────────────────────

def _f(v):
    if v is None or v == '' or v == ' ':
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _load(path: Path):
    rows = []
    with open(path, newline='', encoding='utf-8') as fh:
        for r in csv.DictReader(fh):
            rows.append({
                'symbol': r['symbol'],
                'name': r.get('name', ''),
                'verdict': r['verdict'],
                'market_cap': _f(r.get('market_cap')),
                'dividend_yield': _f(r.get('dividend_yield')),
                'revenue_yoy_growth': _f(r.get('revenue_yoy_growth')),
                'revenue_3y_cagr': _f(r.get('revenue_3y_cagr')),
                'gross_margin_ttm': _f(r.get('gross_margin_ttm')),
                'operating_margin_ttm': _f(r.get('operating_margin_ttm')),
                'free_cash_flow_ttm': _f(r.get('free_cash_flow_ttm')),
                'operating_cash_flow_ttm': _f(r.get('operating_cash_flow_ttm')),
                'rule_40_score': _f(r.get('rule_40_score')),
                'roic_ttm': _f(r.get('roic_ttm')),
            })
    # Derive fcf_margin per row
    for r in rows:
        rev_hint = _f(None)  # screener CSV doesn't carry revenue directly; approximate via FCF/op_inc is ugly
        # We only need fcf_margin when diving into per-group distributions.
        # Skip (can be re-derived if needed).
    return rows


# ─── Classifiers ──────────────────────────────────────────────────────────────

def classify_yoy(r, th):
    """GROWTH if revenue_yoy_growth >= th. UNKNOWN if yoy is None."""
    yoy = r['revenue_yoy_growth']
    if yoy is None:
        return 'UNKNOWN'
    return 'GROWTH' if yoy >= th else 'MATURE'


def classify_cagr(r, th):
    """GROWTH if revenue_3y_cagr >= th. UNKNOWN if cagr is None."""
    cagr = r['revenue_3y_cagr']
    if cagr is None:
        return 'UNKNOWN'
    return 'GROWTH' if cagr >= th else 'MATURE'


def classify_yoy_and_div(r, th, div_th=0.015):
    """GROWTH if yoy >= th AND div_yield < div_th (or div is None).
    MATURE otherwise. UNKNOWN if yoy is None."""
    yoy = r['revenue_yoy_growth']
    div = r['dividend_yield']
    if yoy is None:
        return 'UNKNOWN'
    # div_yield None = treat as non-dividend-payer
    div_low = (div is None) or (div < div_th)
    if yoy >= th and div_low:
        return 'GROWTH'
    return 'MATURE'


CLASSIFIERS = [
    ('A. Revenue YoY only',       classify_yoy),
    ('B. 3y CAGR only',           classify_cagr),
    ('C. YoY + div-yield<1.5%',   classify_yoy_and_div),
]

THRESHOLDS = [0.10, 0.12, 0.15]


# ─── Histogram helpers ────────────────────────────────────────────────────────

def _print_hist(title, values, buckets, width=30):
    print(f"  {title}")
    if not values:
        print(f"    (no values)")
        return
    total = len(values)
    counts = [0] * len(buckets)
    unknowns = 0
    for v in values:
        if v is None:
            unknowns += 1
            continue
        placed = False
        for i, (lo, hi) in enumerate(buckets):
            if lo is None:
                if v < hi:
                    counts[i] += 1
                    placed = True
                    break
            elif hi is None:
                if v >= lo:
                    counts[i] += 1
                    placed = True
                    break
            elif lo <= v < hi:
                counts[i] += 1
                placed = True
                break
        if not placed:
            pass
    known = total - unknowns
    max_count = max(counts) if counts else 1
    print(f"    {'bucket':>12s}  {'count':>6s}  {'%of-known':>9s}  bar")
    for i, (lo, hi) in enumerate(buckets):
        if lo is None:
            label = f"<{hi}"
        elif hi is None:
            label = f">={lo}"
        else:
            label = f"[{lo},{hi})"
        c = counts[i]
        pct = 100.0 * c / known if known else 0
        bar_len = int(width * c / max_count) if max_count else 0
        bar = '#' * bar_len
        print(f"    {label:>12s}  {c:>6d}  {pct:>8.1f}%  {bar}")
    print(f"    (unknown: {unknowns} / {total})")


# Buckets — use decimal for margins/growth, integer for mcap-style
BUCKET_GROWTH = [
    (None, -0.05), (-0.05, 0), (0, 0.05), (0.05, 0.10),
    (0.10, 0.15), (0.15, 0.25), (0.25, 0.50), (0.50, None),
]
BUCKET_MARGIN = [
    (None, 0), (0, 0.05), (0.05, 0.10), (0.10, 0.15),
    (0.15, 0.20), (0.20, 0.30), (0.30, 0.45), (0.45, None),
]
BUCKET_GM = [
    (None, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
    (0.40, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, None),
]
BUCKET_DIV = [
    (None, 0.001), (0.001, 0.010), (0.010, 0.020), (0.020, 0.030),
    (0.030, 0.040), (0.040, 0.060), (0.060, None),
]
BUCKET_ROIC = [
    (None, 0), (0, 0.05), (0.05, 0.10), (0.10, 0.15),
    (0.15, 0.20), (0.20, 0.30), (0.30, None),
]
BUCKET_R40 = [
    (None, 0), (0, 15), (15, 25), (25, 40), (40, 60), (60, None),
]


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found")
        raise SystemExit(1)
    rows = _load(CSV_PATH)
    n = len(rows)
    print('=' * 82)
    print(f"  2-archetype split diagnostic — {CSV_PATH.name} — {n} rows")
    print('=' * 82)

    # (1) Classifier × threshold grid — population split only
    print()
    print('=' * 82)
    print(f"  (1) Population split — Classifier × Threshold")
    print('=' * 82)
    print(f"  {'classifier':<32s}  {'th':>5s}  {'MATURE':>7s}  "
          f"{'GROWTH':>7s}  {'UNKNOWN':>8s}")
    print(f"  {'-'*32}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*8}")
    for name, fn in CLASSIFIERS:
        for th in THRESHOLDS:
            c = Counter(fn(r, th) for r in rows)
            print(f"  {name:<32s}  {th*100:>4.0f}%  "
                  f"{c.get('MATURE', 0):>7d}  {c.get('GROWTH', 0):>7d}  "
                  f"{c.get('UNKNOWN', 0):>8d}")

    # (2) Anchor ticker validation — where do the known names land?
    print()
    print('=' * 82)
    print(f"  (2) Anchor ticker placement — at middle threshold 12%")
    print(f"      [✓ = expected group; ✗ = mismatch; ? = unknown/missing]")
    print('=' * 82)
    by_symbol = {r['symbol']: r for r in rows}

    def _tag(cls, expect):
        if cls == 'UNKNOWN':
            return '?'
        return '✓' if cls == expect else '✗'

    for cname, fn in CLASSIFIERS:
        print()
        print(f"  {cname} @ 12%:")
        for label, expected, anchors in [
            ('MATURE anchors', 'MATURE', ANCHOR_MATURE),
            ('GROWTH anchors', 'GROWTH', ANCHOR_GROWTH),
            ('BORDERLINE',     None,     ANCHOR_BORDERLINE),
        ]:
            print(f"    {label}:")
            for sym in anchors:
                r = by_symbol.get(sym)
                if r is None:
                    print(f"      {sym:<8s} (not in screener_results.csv)")
                    continue
                cls = fn(r, 0.12)
                tag = _tag(cls, expected) if expected else ''
                yoy = r['revenue_yoy_growth']
                cagr = r['revenue_3y_cagr']
                div = r['dividend_yield']
                yoy_s = f"yoy={yoy*100:>5.1f}%" if yoy is not None else "yoy= ?   "
                cagr_s = f"cagr={cagr*100:>5.1f}%" if cagr is not None else "cagr= ?   "
                div_s = f"div={div*100:>4.1f}%" if div is not None else "div= ?  "
                print(f"      {sym:<8s} {tag} -> {cls:<7s}  "
                      f"{yoy_s}  {cagr_s}  {div_s}")

    # (3) Borderline scan — which tickers are closest to the cutoff?
    print()
    print('=' * 82)
    print(f"  (3) Borderline tickers under classifier A @ 12% "
          f"(yoy within ±2pp of cutoff)")
    print('=' * 82)
    borderline = [(r['symbol'], r['name'], r['revenue_yoy_growth'],
                   r['dividend_yield'], r['market_cap'])
                  for r in rows
                  if r['revenue_yoy_growth'] is not None
                  and 0.10 <= r['revenue_yoy_growth'] < 0.14]
    borderline.sort(key=lambda x: x[2])
    print(f"  {len(borderline)} tickers with yoy in [10%, 14%)")
    print(f"  {'sym':<8s}  {'yoy%':>6s}  {'div%':>6s}  {'mcap$B':>8s}  name")
    for sym, name, yoy, div, mcap in borderline[:30]:
        div_s = f"{div*100:>5.2f}" if div is not None else "  ?  "
        mcap_s = f"{mcap/1e9:>7.1f}" if mcap else "   ?   "
        print(f"  {sym:<8s}  {yoy*100:>5.1f}%  {div_s}%  {mcap_s}  {name[:40]}")

    # (4) Per-group distributions under Classifier A @ 12%
    print()
    print('=' * 82)
    print(f"  (4) Per-group distributions — Classifier A "
          f"(Revenue YoY >= 12%)")
    print('=' * 82)
    groups = {'MATURE': [], 'GROWTH': [], 'UNKNOWN': []}
    for r in rows:
        groups[classify_yoy(r, 0.12)].append(r)

    for gname in ['MATURE', 'GROWTH']:
        grp = groups[gname]
        print()
        print('-' * 82)
        print(f"  {gname} — {len(grp)} tickers")
        print('-' * 82)

        print()
        _print_hist('revenue_yoy_growth',
                    [r['revenue_yoy_growth'] for r in grp], BUCKET_GROWTH)
        print()
        _print_hist('revenue_3y_cagr',
                    [r['revenue_3y_cagr'] for r in grp], BUCKET_GROWTH)
        print()
        _print_hist('gross_margin_ttm',
                    [r['gross_margin_ttm'] for r in grp], BUCKET_GM)
        print()
        _print_hist('operating_margin_ttm',
                    [r['operating_margin_ttm'] for r in grp], BUCKET_MARGIN)
        print()
        _print_hist('dividend_yield',
                    [r['dividend_yield'] for r in grp], BUCKET_DIV)
        print()
        _print_hist('roic_ttm',
                    [r['roic_ttm'] for r in grp], BUCKET_ROIC)
        print()
        _print_hist('rule_40_score',
                    [r['rule_40_score'] for r in grp], BUCKET_R40)

    # (5) Verdict distribution per group (where does current rubric land them?)
    print()
    print('=' * 82)
    print(f"  (5) Current verdict distribution per group "
          f"(Classifier A @ 12%)")
    print('=' * 82)
    for gname in ['MATURE', 'GROWTH']:
        grp = groups[gname]
        c = Counter(r['verdict'] for r in grp)
        print()
        print(f"  {gname} (n={len(grp)}):")
        for v in ['INDUSTRY_LEADER', 'HIDDEN_GEM', 'POTENTIAL_LEADER',
                  'WATCH', 'AVOID', 'INSUFFICIENT_DATA']:
            k = c.get(v, 0)
            pct = 100.0 * k / len(grp) if grp else 0
            print(f"    {v:20s}  {k:>5d}  {pct:5.1f}%")

    print()
    print('=' * 82)
    print(f"  Split diagnostic complete.")
    print('=' * 82)


if __name__ == '__main__':
    main()
