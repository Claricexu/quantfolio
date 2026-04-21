"""diag_threshold_sensitivity.py — Phase 1.9 threshold lock-in diagnostic.

Tests Revenue-YoY thresholds T ∈ {10%, 12%, 15%} for the binary
MATURE / GROWTH classifier against the drafted per-archetype rubrics.
Purpose: pick T that maximizes anchor correctness while keeping leader
counts in the target range.

Read-only: reads `screener_results.csv` + anchor lists. No DB writes,
no CSV writes. Outputs tables to stdout.

Usage:
    python diag_threshold_sensitivity.py
"""
from __future__ import annotations

import csv
from pathlib import Path

CSV_PATH = Path(__file__).parent / 'screener_results.csv'

# Anchor lists — "if the classifier is working, these must land here."
ANCHOR_MATURE = ['KO', 'JNJ', 'CVX', 'PG', 'WMT', 'XOM', 'VZ', 'HD', 'MMM',
                 'DIS', 'F', 'GM', 'DUK', 'NEE', 'SO', 'MCD', 'PEP', 'T', 'IBM']
ANCHOR_GROWTH = ['NOW', 'CRWD', 'SNOW', 'NVDA', 'CRM', 'DDOG', 'ZS', 'PANW',
                 'PLTR', 'NFLX', 'TSLA', 'ANET']
ANCHOR_BORDERLINE = ['MSFT', 'META', 'AAPL', 'AMZN', 'GOOGL', 'LLY']

THRESHOLDS = [0.10, 0.12, 0.15]

VERDICTS = ['LEADER', 'GEM', 'WATCH', 'AVOID', 'INSUFFICIENT_DATA']


# ─── CSV loader ──────────────────────────────────────────────────────────────

def _f(s):
    if s is None or s == '':
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v:  # NaN
        return None
    return v


def _i(s):
    if s is None or s == '':
        return None
    try:
        v = int(float(s))
    except ValueError:
        return None
    return v


def load_rows():
    with CSV_PATH.open('r', encoding='utf-8', newline='') as fh:
        reader = csv.DictReader(fh)
        out = []
        for r in reader:
            out.append({
                'symbol': r['symbol'],
                'revenue_yoy_growth': _f(r.get('revenue_yoy_growth')),
                'revenue_3y_cagr': _f(r.get('revenue_3y_cagr')),
                'growth_trajectory': (r.get('growth_trajectory') or '').strip(),
                'gross_margin_ttm': _f(r.get('gross_margin_ttm')),
                'operating_margin_ttm': _f(r.get('operating_margin_ttm')),
                'operating_cash_flow_ttm': _f(r.get('operating_cash_flow_ttm')),
                'free_cash_flow_ttm': _f(r.get('free_cash_flow_ttm')),
                'rule_40_score': _f(r.get('rule_40_score')),
                'roic_ttm': _f(r.get('roic_ttm')),
                'market_cap_rank_in_sector': _i(r.get('market_cap_rank_in_sector')),
                'flag_diluting': _i(r.get('flag_diluting')) or 0,
                'flag_burning_cash': _i(r.get('flag_burning_cash')) or 0,
                'flag_spac_or_microcap': _i(r.get('flag_spac_or_microcap')) or 0,
            })
    return out


def _fcf_margin(m):
    """Back-compute fcf_margin from `rule_40 = (rev_yoy + fcf_margin) * 100`.
    Returns None if either rule_40 or yoy is unavailable."""
    r40 = m['rule_40_score']
    yoy = m['revenue_yoy_growth']
    if r40 is None or yoy is None:
        return None
    return r40 / 100.0 - yoy


# ─── Classifier ──────────────────────────────────────────────────────────────

def classify(m, T):
    g = m['revenue_yoy_growth']
    if g is None:
        return 'UNKNOWN'
    return 'GROWTH' if g >= T else 'MATURE'


# ─── MATURE rubric (5 tests) ────────────────────────────────────────────────

def m_not_declining(m):
    c = m['revenue_3y_cagr']
    if c is None:
        return None
    return c >= 0.0


def m_margin_quality(m):
    om = m['operating_margin_ttm']
    if om is None:
        return None
    return om >= 0.10


def m_cash_generation(m):
    ocf = m['operating_cash_flow_ttm']
    fcfm = _fcf_margin(m)
    if ocf is None or fcfm is None:
        return None
    if ocf <= 0:
        return False
    if fcfm < 0.08:
        return False
    return True


def m_moat(m):
    roic = m['roic_ttm']
    rank = m['market_cap_rank_in_sector']
    if roic is None and rank is None:
        return None
    if roic is not None and roic >= 0.10:
        return True
    if rank is not None and rank <= 5:
        return True
    return False


def m_stability(m):
    trj = m['growth_trajectory']
    if trj == '' or trj is None:
        return None
    if trj == 'decelerating':
        return False
    if m['flag_diluting'] == 1:
        return False
    return True


MATURE_TESTS = [
    ('not_declining', m_not_declining),
    ('margin_quality', m_margin_quality),
    ('cash_generation', m_cash_generation),
    ('moat', m_moat),
    ('stability', m_stability),
]


# ─── GROWTH rubric (5 tests) ─────────────────────────────────────────────────

def g_growth_rate(m):
    yoy = m['revenue_yoy_growth']
    trj = m['growth_trajectory']
    if yoy is None:
        return None
    if yoy < 0.15:
        return False
    if trj == 'decelerating':
        return False
    return True


def g_unit_economics(m):
    gm = m['gross_margin_ttm']
    if gm is None:
        return None
    return gm >= 0.50


def g_path_to_profits(m):
    ocf = m['operating_cash_flow_ttm']
    r40 = m['rule_40_score']
    if ocf is None and r40 is None:
        return None
    if ocf is not None and ocf > 0:
        return True
    if r40 is not None and r40 >= 40.0:
        return True
    return False


def g_moat(m):
    return m_moat(m)  # same definition as MATURE


def g_capital_efficiency(m):
    r40 = m['rule_40_score']
    if r40 is None:
        return None
    return r40 >= 40.0


GROWTH_TESTS = [
    ('growth_rate', g_growth_rate),
    ('unit_economics', g_unit_economics),
    ('path_to_profits', g_path_to_profits),
    ('moat', g_moat),
    ('capital_efficiency', g_capital_efficiency),
]


# ─── Dealbreakers per archetype ──────────────────────────────────────────────

def mature_dealbreaker(m):
    """Return reason string if fired, else None."""
    c = m['revenue_3y_cagr']
    if c is not None and c < -0.05:
        return 'cagr_shrinking'
    if m['flag_diluting'] == 1:
        return 'diluting'
    return None


def growth_dealbreaker(m):
    if m['flag_burning_cash'] == 1:
        return 'burning_cash'
    if m['flag_diluting'] == 1:
        return 'diluting'
    return None


# ─── Verdict ─────────────────────────────────────────────────────────────────

def verdict(m, archetype):
    if archetype == 'UNKNOWN':
        return 'INSUFFICIENT_DATA'
    tests = MATURE_TESTS if archetype == 'MATURE' else GROWTH_TESTS
    db_fn = mature_dealbreaker if archetype == 'MATURE' else growth_dealbreaker

    results = [fn(m) for _, fn in tests]
    passes = sum(1 for r in results if r is True)
    known = sum(1 for r in results if r is not None)

    if known < 3:
        return 'INSUFFICIENT_DATA'
    if db_fn(m) is not None:
        return 'AVOID'
    if passes <= 2:
        return 'AVOID'
    if passes == 5:
        rank = m['market_cap_rank_in_sector']
        if rank is not None and rank <= 5:
            return 'LEADER'
        return 'GEM'
    return 'WATCH'


# ─── Reporting ───────────────────────────────────────────────────────────────

def _fmt_v(d, order=VERDICTS):
    parts = [f'{k}={d[k]}' for k in order if d.get(k, 0) > 0]
    return '  '.join(parts) if parts else '(empty)'


def _fmt_pct(x):
    if x is None:
        return '   n/a'
    return f'{x*100:+6.1f}%'


def _anchor_line(sym, row, archetype, v):
    if row is None:
        return f'    {sym:6s}  (not in screener_results.csv)'
    yoy = _fmt_pct(row['revenue_yoy_growth'])
    arch_code = archetype[0] if archetype else '?'  # M/G/U
    return f'    {sym:6s}  yoy={yoy}  arch={archetype:7s}  verdict={v}'


def run_sensitivity():
    rows = load_rows()
    index = {r['symbol']: r for r in rows}

    print('=' * 80)
    print(f'  Phase 1.9 threshold sensitivity — {len(rows)} rows from '
          'screener_results.csv')
    print('=' * 80)

    all_anchors = ANCHOR_MATURE + ANCHOR_GROWTH + ANCHOR_BORDERLINE
    missing = [s for s in all_anchors if s not in index]
    if missing:
        print(f'  WARNING: {len(missing)} anchor(s) missing from screener_results: '
              f'{missing}')
        print('           (likely missing YoY data — classifier cannot evaluate)')
        print()

    for T in THRESHOLDS:
        print()
        print('━' * 80)
        print(f'  T = {T*100:.0f}%  (revenue_yoy < {T*100:.0f}% → MATURE; '
              f'else → GROWTH)')
        print('━' * 80)

        # Classify all rows
        arch = {r['symbol']: classify(r, T) for r in rows}

        # ── (A) Population split ────
        split = {'MATURE': 0, 'GROWTH': 0, 'UNKNOWN': 0}
        for a in arch.values():
            split[a] += 1
        total_known = split['MATURE'] + split['GROWTH']
        pct_m = 100.0 * split['MATURE'] / total_known if total_known else 0
        pct_g = 100.0 * split['GROWTH'] / total_known if total_known else 0
        print(f'\n  (A) Population split:')
        print(f'      MATURE  = {split["MATURE"]:4d}  ({pct_m:5.1f}% of known)')
        print(f'      GROWTH  = {split["GROWTH"]:4d}  ({pct_g:5.1f}% of known)')
        print(f'      UNKNOWN = {split["UNKNOWN"]:4d}')

        # ── (B) Verdict distribution per archetype ────
        m_verdicts = {k: 0 for k in VERDICTS}
        g_verdicts = {k: 0 for k in VERDICTS}
        for r in rows:
            a = arch[r['symbol']]
            v = verdict(r, a)
            if a == 'MATURE':
                m_verdicts[v] += 1
            elif a == 'GROWTH':
                g_verdicts[v] += 1
        print(f'\n  (B) Verdict distribution (drafted rubrics):')
        print(f'      MATURE : {_fmt_v(m_verdicts)}')
        print(f'      GROWTH : {_fmt_v(g_verdicts)}')
        total_L = m_verdicts['LEADER'] + g_verdicts['LEADER']
        total_G = m_verdicts['GEM'] + g_verdicts['GEM']
        total_W = m_verdicts['WATCH'] + g_verdicts['WATCH']
        total_A = m_verdicts['AVOID'] + g_verdicts['AVOID']
        print(f'      TOTAL  : LEADER={total_L}  GEM={total_G}  '
              f'WATCH={total_W}  AVOID={total_A}')
        print(f'               leaders.csv pool = LEADER ∪ GEM = {total_L+total_G}')

        # ── (C) Anchor landing + verdict ────
        print(f'\n  (C) Anchor results (where each anchor lands + verdict):')

        def _show_anchors(label, syms, expected=None):
            print(f'\n      {label}:')
            for s in syms:
                row = index.get(s)
                if row is None:
                    print(f'    {s:6s}  (not in screener_results.csv)')
                    continue
                a = arch[s]
                v = verdict(row, a)
                flag = ''
                if expected and a != expected and a != 'UNKNOWN':
                    flag = '  ← WRONG ARCHETYPE'
                print(_anchor_line(s, row, a, v) + flag)

        _show_anchors('ANCHOR_MATURE (expected: MATURE)', ANCHOR_MATURE,
                      expected='MATURE')
        _show_anchors('ANCHOR_GROWTH (expected: GROWTH)', ANCHOR_GROWTH,
                      expected='GROWTH')
        _show_anchors('ANCHOR_BORDERLINE (tabulated — no expectation)',
                      ANCHOR_BORDERLINE)

    # ── Summary matrix ────
    print()
    print('=' * 80)
    print('  Summary matrix (pick T that best balances anchor correctness '
          'vs leader count)')
    print('=' * 80)
    print(f"  {'T':>4s} | {'MATURE':>7s} | {'GROWTH':>7s} | "
          f"{'LEADER':>7s} | {'GEM':>5s} | {'LEADER∪GEM':>11s} | "
          f"{'mat_ok':>7s}/{'grw_ok':>7s}")
    for T in THRESHOLDS:
        arch = {r['symbol']: classify(r, T) for r in rows}
        split = {'MATURE': 0, 'GROWTH': 0, 'UNKNOWN': 0}
        for a in arch.values():
            split[a] += 1
        m_v = {k: 0 for k in VERDICTS}
        g_v = {k: 0 for k in VERDICTS}
        for r in rows:
            a = arch[r['symbol']]
            v = verdict(r, a)
            if a == 'MATURE':
                m_v[v] += 1
            elif a == 'GROWTH':
                g_v[v] += 1
        total_L = m_v['LEADER'] + g_v['LEADER']
        total_G = m_v['GEM'] + g_v['GEM']
        mat_ok = sum(1 for s in ANCHOR_MATURE
                     if s in index and arch[s] == 'MATURE')
        grw_ok = sum(1 for s in ANCHOR_GROWTH
                     if s in index and arch[s] == 'GROWTH')
        mat_n = sum(1 for s in ANCHOR_MATURE if s in index)
        grw_n = sum(1 for s in ANCHOR_GROWTH if s in index)
        print(f"  {T*100:3.0f}% | {split['MATURE']:7d} | {split['GROWTH']:7d} | "
              f"{total_L:7d} | {total_G:5d} | {total_L+total_G:11d} | "
              f"{mat_ok:>3d}/{mat_n:<3d} | {grw_ok:>3d}/{grw_n:<3d}")


if __name__ == '__main__':
    run_sensitivity()
