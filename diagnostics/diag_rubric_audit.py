"""Phase 1.8 Step 1 — Rubric audit diagnostic.

Read-only characterization of the Good Firm verdict rubric over the current
`screener_results.csv`. Answers:

  (A) Per-test pass / fail / unknown counts — which gate is binding?
  (B) Dealbreaker flag frequencies — how often does each fire?
  (C) ROIC histogram — where is the mass relative to the 10% bar?
  (D) R40 histogram — where is the mass relative to the 25 bar?
  (E) AVOID decomposition — dealbreaker-gated vs test-gated, and within each
      which flag / which test is the dominant cause.
  (F) Rank-2-by-sector audit — for the ~180 tickers eligible for the moat
      `rank<=2` bypass, which test do they actually fail?

No DB writes, no CSV writes. Prints tables to stdout. Pure local, ~5 sec.

Usage:
    python diag_rubric_audit.py

Mirrors the test logic in `fundamental_screener.py` so the audit reflects what
the rubric actually does today (not what we plan to change).
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

CSV_PATH = Path(__file__).parent / 'screener_results.csv'


# ─── Replay of fundamental_screener.py test logic ─────────────────────────────
# Kept in sync with `_test_*` in fundamental_screener.py. If any threshold
# changes there, this diagnostic must be updated in lockstep.

def _test_growth(m):
    yoy = m.get('revenue_yoy_growth')
    if yoy is None:
        return None
    if yoy < 0.10:
        return False
    if m.get('growth_trajectory') == 'decelerating':
        return False
    return True


def _test_business_model(m):
    gm = m.get('gross_margin_ttm')
    if gm is None:
        return None
    if gm < 0.40:
        return False
    om = m.get('operating_margin_ttm')
    if om is not None and om < 0:
        return False
    return True


def _test_profitability(m):
    ocf = m.get('operating_cash_flow_ttm')
    if ocf is None:
        return None
    if ocf <= 0:
        return False
    fcf = m.get('free_cash_flow_ttm')
    if fcf is not None and fcf <= 0:
        return False
    return True


def _test_moat(m):
    roic = m.get('roic_ttm')
    rank = m.get('market_cap_rank_in_sector')
    if roic is None and rank is None:
        return None
    if roic is not None and roic >= 0.10:
        return True
    if rank is not None and rank <= 2:
        return True
    return False


def _test_capital_efficiency(m):
    r40 = m.get('rule_40_score')
    if r40 is None:
        return None
    return r40 >= 25.0


TESTS = [
    ('growth', _test_growth),
    ('business_model', _test_business_model),
    ('profitability', _test_profitability),
    ('moat', _test_moat),
    ('capital_efficiency', _test_capital_efficiency),
]


# ─── CSV parsing helpers ──────────────────────────────────────────────────────

def _f(v):
    """CSV cell → float | None. NaN literals (rare but possible) → None so
    downstream comparisons match fundamental_screener.py's None semantics."""
    if v is None or v == '' or v == ' ':
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _i(v):
    """CSV cell → int (for 0/1 flag columns). None if unparseable or NaN."""
    f = _f(v)
    if f is None:
        return None
    # Guard against NaN (float('nan') parses but int(nan) raises ValueError)
    if f != f:
        return None
    return int(f)


def _s(v):
    """CSV cell → string | None."""
    if v is None or v == '':
        return None
    return v


def _load(path: Path):
    rows = []
    with open(path, newline='', encoding='utf-8') as fh:
        for r in csv.DictReader(fh):
            rows.append({
                'symbol': r['symbol'],
                'verdict': r['verdict'],
                'sic': _s(r.get('sic')),
                'market_cap': _f(r.get('market_cap')),
                'revenue_yoy_growth': _f(r.get('revenue_yoy_growth')),
                'growth_trajectory': _s(r.get('growth_trajectory')),
                'gross_margin_ttm': _f(r.get('gross_margin_ttm')),
                'operating_margin_ttm': _f(r.get('operating_margin_ttm')),
                'operating_cash_flow_ttm': _f(r.get('operating_cash_flow_ttm')),
                'free_cash_flow_ttm': _f(r.get('free_cash_flow_ttm')),
                'rule_40_score': _f(r.get('rule_40_score')),
                'roic_ttm': _f(r.get('roic_ttm')),
                'market_cap_rank_in_sector': _f(r.get('market_cap_rank_in_sector')),
                'flag_diluting': _i(r.get('flag_diluting')) or 0,
                'flag_burning_cash': _i(r.get('flag_burning_cash')) or 0,
                'flag_spac_or_microcap': _i(r.get('flag_spac_or_microcap')) or 0,
            })
    return rows


# ─── Bucket helpers ───────────────────────────────────────────────────────────

ROIC_BUCKETS = [
    ('?',       lambda v: v is None),
    ('<0',      lambda v: v < 0),
    ('0-5%',    lambda v: 0 <= v < 0.05),
    ('5-10%',   lambda v: 0.05 <= v < 0.10),
    ('10-15%',  lambda v: 0.10 <= v < 0.15),
    ('15-20%',  lambda v: 0.15 <= v < 0.20),
    ('20-30%',  lambda v: 0.20 <= v < 0.30),
    ('>=30%',   lambda v: v >= 0.30),
]

R40_BUCKETS = [
    ('?',       lambda v: v is None),
    ('<0',      lambda v: v < 0),
    ('0-15',    lambda v: 0 <= v < 15),
    ('15-25',   lambda v: 15 <= v < 25),
    ('25-40',   lambda v: 25 <= v < 40),
    ('40-60',   lambda v: 40 <= v < 60),
    ('>=60',    lambda v: v >= 60),
]


def _bucketize(values, buckets):
    c = Counter()
    for v in values:
        for label, pred in buckets:
            try:
                if pred(v):
                    c[label] += 1
                    break
            except TypeError:
                continue
    return c


def _print_bucket_table(title, counter, order, total):
    print(f"  {title}")
    print(f"  {'bucket':>8s}  {'count':>6s}  {'pct':>7s}  bar")
    for label in order:
        n = counter.get(label, 0)
        pct = 100.0 * n / total if total else 0
        bar = '#' * int(pct)
        print(f"  {label:>8s}  {n:>6d}  {pct:>6.1f}%  {bar}")


# ─── Main audit ───────────────────────────────────────────────────────────────

def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found — run fundamental_screener.py first")
        raise SystemExit(1)

    rows = _load(CSV_PATH)
    n = len(rows)
    print('=' * 80)
    print(f"  Rubric audit — {CSV_PATH.name} — {n} rows")
    print('=' * 80)

    # ── (0) Verdict distribution sanity check ────────────────────────────────
    print()
    print(f"  (0) Verdict distribution (sanity check)")
    c_verdict = Counter(r['verdict'] for r in rows)
    for v in ['INDUSTRY_LEADER', 'HIDDEN_GEM', 'POTENTIAL_LEADER',
              'WATCH', 'AVOID', 'INSUFFICIENT_DATA']:
        k = c_verdict.get(v, 0)
        pct = 100.0 * k / n if n else 0
        print(f"    {v:20s} {k:>5d}  {pct:5.1f}%")

    # ── (A) Per-test pass / fail / unknown ───────────────────────────────────
    print()
    print('=' * 80)
    print(f"  (A) Per-test breakdown — which gate is binding?")
    print('=' * 80)
    print(f"  {'test':20s}  {'pass':>6s}  {'fail':>6s}  {'?':>6s}  "
          f"{'pass%':>6s}  {'fail%':>6s}  {'?%':>6s}")
    per_test = {name: Counter() for name, _ in TESTS}
    for r in rows:
        for name, fn in TESTS:
            res = fn(r)
            if res is True:
                per_test[name]['pass'] += 1
            elif res is False:
                per_test[name]['fail'] += 1
            else:
                per_test[name]['unknown'] += 1
    for name, _ in TESTS:
        c = per_test[name]
        p, f, u = c['pass'], c['fail'], c['unknown']
        total = p + f + u
        print(f"  {name:20s}  {p:>6d}  {f:>6d}  {u:>6d}  "
              f"{100*p/total:>5.1f}%  {100*f/total:>5.1f}%  {100*u/total:>5.1f}%")

    # ── (B) Dealbreaker flag frequencies ─────────────────────────────────────
    print()
    print('=' * 80)
    print(f"  (B) Dealbreaker flag frequencies")
    print('=' * 80)
    print(f"  (flags are binary vetoes; any one = AVOID regardless of tests)")
    flag_counts = {
        'flag_diluting':         sum(1 for r in rows if r['flag_diluting']),
        'flag_burning_cash':     sum(1 for r in rows if r['flag_burning_cash']),
        'flag_spac_or_microcap': sum(1 for r in rows if r['flag_spac_or_microcap']),
    }
    any_flag = sum(1 for r in rows
                   if r['flag_diluting'] or r['flag_burning_cash']
                   or r['flag_spac_or_microcap'])
    for fname, k in flag_counts.items():
        pct = 100.0 * k / n if n else 0
        print(f"    {fname:25s} {k:>5d}  {pct:5.1f}%")
    print(f"    {'(any dealbreaker)':25s} {any_flag:>5d}  {100.0*any_flag/n:5.1f}%")

    # ── (C) ROIC histogram ───────────────────────────────────────────────────
    print()
    print('=' * 80)
    print(f"  (C) ROIC histogram — moat test bar = 10%")
    print('=' * 80)
    roic_hist = _bucketize((r['roic_ttm'] for r in rows), ROIC_BUCKETS)
    _print_bucket_table("roic_ttm", roic_hist,
                        [b[0] for b in ROIC_BUCKETS], n)
    # How many tickers cross the 10% bar?
    roic_vals = [r['roic_ttm'] for r in rows if r['roic_ttm'] is not None]
    above10 = sum(1 for v in roic_vals if v >= 0.10)
    print(f"\n    known: {len(roic_vals)}  |  >=10%: {above10} "
          f"({100*above10/len(roic_vals):.1f}% of known)")

    # ── (D) R40 histogram ────────────────────────────────────────────────────
    print()
    print('=' * 80)
    print(f"  (D) Rule-of-40 histogram — capital_efficiency test bar = 25")
    print('=' * 80)
    r40_hist = _bucketize((r['rule_40_score'] for r in rows), R40_BUCKETS)
    _print_bucket_table("rule_40", r40_hist,
                        [b[0] for b in R40_BUCKETS], n)
    r40_vals = [r['rule_40_score'] for r in rows if r['rule_40_score'] is not None]
    above25 = sum(1 for v in r40_vals if v >= 25)
    above40 = sum(1 for v in r40_vals if v >= 40)
    print(f"\n    known: {len(r40_vals)}  |  >=25: {above25} "
          f"({100*above25/len(r40_vals):.1f}% of known)"
          f"  |  >=40: {above40} ({100*above40/len(r40_vals):.1f}% of known)")

    # ── (E) AVOID decomposition ──────────────────────────────────────────────
    print()
    print('=' * 80)
    print(f"  (E) AVOID decomposition — dealbreaker-gated vs test-gated")
    print('=' * 80)
    avoids = [r for r in rows if r['verdict'] == 'AVOID']
    n_avoid = len(avoids)

    flag_only = 0        # dealbreaker fires, tests would have passed >=3
    test_only = 0        # no dealbreaker, passes <=2
    both = 0             # dealbreaker fires AND passes <=2
    for r in avoids:
        has_flag = bool(r['flag_diluting'] or r['flag_burning_cash']
                        or r['flag_spac_or_microcap'])
        passes = sum(1 for name, fn in TESTS if fn(r) is True)
        if has_flag and passes <= 2:
            both += 1
        elif has_flag:
            flag_only += 1
        else:
            test_only += 1
    print(f"    AVOID total:              {n_avoid}")
    print(f"    dealbreaker only:         {flag_only:>5d}  "
          f"({100*flag_only/n_avoid:.1f}% of AVOID)")
    print(f"    test-gated only (≤2 pass): {test_only:>5d}  "
          f"({100*test_only/n_avoid:.1f}% of AVOID)")
    print(f"    both:                     {both:>5d}  "
          f"({100*both/n_avoid:.1f}% of AVOID)")

    # Within dealbreaker AVOIDs, which flag fires most?
    print()
    print(f"    Within {flag_only + both} dealbreaker-gated AVOIDs:")
    dk_dil = sum(1 for r in avoids if r['flag_diluting'])
    dk_burn = sum(1 for r in avoids if r['flag_burning_cash'])
    dk_spac = sum(1 for r in avoids if r['flag_spac_or_microcap'])
    print(f"      flag_diluting:         {dk_dil}")
    print(f"      flag_burning_cash:     {dk_burn}")
    print(f"      flag_spac_or_microcap: {dk_spac}")

    # Within test-gated AVOIDs, which test fails most?
    print()
    print(f"    Within {test_only} test-gated AVOIDs (no dealbreaker, ≤2 pass):")
    test_fails = Counter()
    for r in avoids:
        has_flag = bool(r['flag_diluting'] or r['flag_burning_cash']
                        or r['flag_spac_or_microcap'])
        if has_flag:
            continue
        for name, fn in TESTS:
            res = fn(r)
            if res is False:
                test_fails[name + ':fail'] += 1
            elif res is None:
                test_fails[name + ':?'] += 1
    for name, _ in TESTS:
        f = test_fails.get(name + ':fail', 0)
        u = test_fails.get(name + ':?', 0)
        print(f"      {name:20s}  fail={f:>4d}  unknown={u:>4d}")

    # ── (F) Rank-2-by-sector audit — moat bypass reality check ───────────────
    print()
    print('=' * 80)
    print(f"  (F) Rank-2-by-sector audit — how do moat-bypass eligibles fare?")
    print('=' * 80)
    rank2 = [r for r in rows
             if r['market_cap_rank_in_sector'] is not None
             and r['market_cap_rank_in_sector'] <= 2]
    print(f"    eligible (rank<=2 in SIC-2 sector): {len(rank2)}")
    c_r2_verdict = Counter(r['verdict'] for r in rank2)
    for v in ['INDUSTRY_LEADER', 'HIDDEN_GEM', 'POTENTIAL_LEADER',
              'WATCH', 'AVOID', 'INSUFFICIENT_DATA']:
        k = c_r2_verdict.get(v, 0)
        pct = 100.0 * k / len(rank2) if rank2 else 0
        print(f"    {v:20s}  {k:>4d}  {pct:5.1f}%")

    # For rank<=2 that aren't IL, which test keeps them out?
    print()
    print(f"    Rank<=2 tickers NOT tagged INDUSTRY_LEADER — which test fails?")
    blockers = Counter()
    for r in rank2:
        if r['verdict'] == 'INDUSTRY_LEADER':
            continue
        for name, fn in TESTS:
            res = fn(r)
            if res is False:
                blockers[name + ':fail'] += 1
            elif res is None:
                blockers[name + ':?'] += 1
    for name, _ in TESTS:
        f = blockers.get(name + ':fail', 0)
        u = blockers.get(name + ':?', 0)
        print(f"      {name:20s}  fail={f:>4d}  unknown={u:>4d}")

    print()
    print('=' * 80)
    print(f"  Audit complete.")
    print('=' * 80)


if __name__ == '__main__':
    main()
