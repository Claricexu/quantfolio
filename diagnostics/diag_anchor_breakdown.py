"""diag_anchor_breakdown.py — Phase 1.9 pre-implementation investigation.

For each of the 37 anchor tickers (MATURE + GROWTH + BORDERLINE), show:
  1. Classified archetype at T=12%
  2. Result + reason for every test in that archetype's rubric
  3. Result + reason for every dealbreaker (whether fired or not)
  4. Final verdict

Purpose: confirm whether the NOW / NFLX / PANW → AVOID outcome (and
CVX / XOM / GM → AVOID) is driven by the right reasons — i.e. the
dealbreaker we intend, not a metric glitch — before we lock the rubric
into `fundamental_screener.py`.

Read-only. Reads `screener_results.csv`. No writes.

Usage:
    python diag_anchor_breakdown.py
"""
from __future__ import annotations

import csv
from pathlib import Path

CSV_PATH = Path(__file__).parent / 'screener_results.csv'

T_LOCKED = 0.12  # Locked by user 2026-04-19

ANCHOR_MATURE = ['KO', 'JNJ', 'CVX', 'PG', 'WMT', 'XOM', 'VZ', 'HD', 'MMM',
                 'DIS', 'F', 'GM', 'DUK', 'NEE', 'SO', 'MCD', 'PEP', 'T', 'IBM']
ANCHOR_GROWTH = ['NOW', 'CRWD', 'SNOW', 'NVDA', 'CRM', 'DDOG', 'ZS', 'PANW',
                 'PLTR', 'NFLX', 'TSLA', 'ANET']
ANCHOR_BORDERLINE = ['MSFT', 'META', 'AAPL', 'AMZN', 'GOOGL', 'LLY']

ALL_ANCHORS = ANCHOR_MATURE + ANCHOR_GROWTH + ANCHOR_BORDERLINE


# ─── CSV loader (same as diag_threshold_sensitivity.py) ──────────────────────

def _f(s):
    if s is None or s == '':
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v:
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
    r40 = m['rule_40_score']
    yoy = m['revenue_yoy_growth']
    if r40 is None or yoy is None:
        return None
    return r40 / 100.0 - yoy


# ─── Formatters ──────────────────────────────────────────────────────────────

def _pct(x):
    if x is None:
        return '  n/a'
    return f'{x*100:+6.1f}%'


def _num(x):
    if x is None:
        return '    n/a'
    if abs(x) >= 1e9:
        return f'{x/1e9:+7.2f}B'
    if abs(x) >= 1e6:
        return f'{x/1e6:+7.1f}M'
    return f'{x:+8.2f}'


def _mark(r):
    if r is True:
        return 'PASS'
    if r is False:
        return 'FAIL'
    return 'unk '


# ─── MATURE tests with reason strings ───────────────────────────────────────

def m_not_declining(m):
    c = m['revenue_3y_cagr']
    if c is None:
        return None, 'revenue_3y_cagr=n/a'
    passed = c >= 0.0
    return passed, f'revenue_3y_cagr={_pct(c)} {"≥" if passed else "<"} 0%'


def m_margin_quality(m):
    om = m['operating_margin_ttm']
    if om is None:
        return None, 'operating_margin_ttm=n/a'
    passed = om >= 0.10
    return passed, f'operating_margin={_pct(om)} {"≥" if passed else "<"} 10%'


def m_cash_generation(m):
    ocf = m['operating_cash_flow_ttm']
    fcfm = _fcf_margin(m)
    if ocf is None or fcfm is None:
        return None, (f'ocf={_num(ocf)} fcf_margin={_pct(fcfm)}  '
                      '(need both known)')
    if ocf <= 0:
        return False, f'ocf={_num(ocf)} ≤ 0'
    if fcfm < 0.08:
        return False, f'fcf_margin={_pct(fcfm)} < 8%  (ocf={_num(ocf)} ok)'
    return True, f'ocf={_num(ocf)} >0 AND fcf_margin={_pct(fcfm)} ≥ 8%'


def m_moat(m):
    roic = m['roic_ttm']
    rank = m['market_cap_rank_in_sector']
    if roic is None and rank is None:
        return None, 'roic=n/a rank=n/a'
    if roic is not None and roic >= 0.10:
        return True, f'roic={_pct(roic)} ≥ 10%  (rank={rank})'
    if rank is not None and rank <= 5:
        return True, f'rank={rank} ≤ 5  (roic={_pct(roic)})'
    return False, f'roic={_pct(roic)} < 10% AND rank={rank} > 5'


def m_stability(m):
    trj = m['growth_trajectory']
    dil = m['flag_diluting']
    if trj == '' or trj is None:
        return None, 'growth_trajectory=n/a'
    if trj == 'decelerating':
        return False, f"growth_trajectory='decelerating'"
    if dil == 1:
        return False, f'flag_diluting=1 (trj={trj})'
    return True, f'trj={trj} AND flag_diluting=0'


MATURE_TESTS = [
    ('not_declining', m_not_declining),
    ('margin_quality', m_margin_quality),
    ('cash_generation', m_cash_generation),
    ('moat', m_moat),
    ('stability', m_stability),
]


# ─── GROWTH tests ────────────────────────────────────────────────────────────

def g_growth_rate(m):
    yoy = m['revenue_yoy_growth']
    trj = m['growth_trajectory']
    if yoy is None:
        return None, 'revenue_yoy_growth=n/a'
    if yoy < 0.15:
        return False, f'yoy={_pct(yoy)} < 15%'
    if trj == 'decelerating':
        return False, f"yoy={_pct(yoy)} ≥ 15% BUT trj='decelerating'"
    return True, f'yoy={_pct(yoy)} ≥ 15% AND trj={trj}'


def g_unit_economics(m):
    gm = m['gross_margin_ttm']
    if gm is None:
        return None, 'gross_margin_ttm=n/a'
    passed = gm >= 0.50
    return passed, f'gross_margin={_pct(gm)} {"≥" if passed else "<"} 50%'


def g_path_to_profits(m):
    ocf = m['operating_cash_flow_ttm']
    r40 = m['rule_40_score']
    if ocf is None and r40 is None:
        return None, 'ocf=n/a r40=n/a'
    if ocf is not None and ocf > 0:
        return True, f'ocf={_num(ocf)} > 0  (r40={r40})'
    if r40 is not None and r40 >= 40.0:
        return True, f'r40={r40:.1f} ≥ 40  (ocf={_num(ocf)})'
    return False, f'ocf={_num(ocf)} ≤ 0 AND r40={r40} < 40'


def g_moat(m):
    return m_moat(m)


def g_capital_efficiency(m):
    r40 = m['rule_40_score']
    if r40 is None:
        return None, 'rule_40_score=n/a'
    passed = r40 >= 40.0
    return passed, f'r40={r40:.1f} {"≥" if passed else "<"} 40'


GROWTH_TESTS = [
    ('growth_rate', g_growth_rate),
    ('unit_economics', g_unit_economics),
    ('path_to_profits', g_path_to_profits),
    ('moat', g_moat),
    ('capital_efficiency', g_capital_efficiency),
]


# ─── Dealbreakers with reason ───────────────────────────────────────────────

def mature_dealbreakers(m):
    """Yield (name, fired, reason) tuples."""
    c = m['revenue_3y_cagr']
    if c is not None and c < -0.05:
        yield ('cagr_shrinking', True, f'revenue_3y_cagr={_pct(c)} < -5%')
    else:
        yield ('cagr_shrinking', False,
               f'revenue_3y_cagr={_pct(c)} {"≥ -5%" if c is not None else "unknown, skipped"}')
    yield ('diluting', m['flag_diluting'] == 1,
           f'flag_diluting={m["flag_diluting"]}')


def growth_dealbreakers(m):
    yield ('burning_cash', m['flag_burning_cash'] == 1,
           f'flag_burning_cash={m["flag_burning_cash"]}')
    yield ('diluting', m['flag_diluting'] == 1,
           f'flag_diluting={m["flag_diluting"]}')


# ─── Per-anchor report ──────────────────────────────────────────────────────

def classify(m, T):
    g = m['revenue_yoy_growth']
    if g is None:
        return 'UNKNOWN'
    return 'GROWTH' if g >= T else 'MATURE'


def report_anchor(sym, row):
    if row is None:
        print(f'\n── {sym} ──  (not in screener_results.csv)')
        return

    arch = classify(row, T_LOCKED)
    print(f'\n── {sym}  (archetype={arch},  yoy={_pct(row["revenue_yoy_growth"])}, '
          f'rank={row["market_cap_rank_in_sector"]}) ──')

    if arch == 'UNKNOWN':
        print('  verdict = INSUFFICIENT_DATA  (no revenue_yoy_growth)')
        return

    tests = MATURE_TESTS if arch == 'MATURE' else GROWTH_TESTS
    db_iter = mature_dealbreakers if arch == 'MATURE' else growth_dealbreakers

    results = []
    for name, fn in tests:
        r, reason = fn(row)
        results.append((name, r))
        print(f'  {_mark(r):>4s}  {name:18s}  {reason}')

    passes = sum(1 for _, r in results if r is True)
    known = sum(1 for _, r in results if r is not None)
    print(f'        passes={passes}/{known} known of 5')

    print(f'  -- dealbreakers --')
    any_fired = False
    for name, fired, reason in db_iter(row):
        tag = '🔥FIRE' if fired else '  ok  '
        if fired:
            any_fired = True
        print(f'  {tag}  {name:18s}  {reason}')

    # Verdict
    if known < 3:
        v = 'INSUFFICIENT_DATA'
    elif any_fired:
        v = 'AVOID'
    elif passes <= 2:
        v = 'AVOID'
    elif passes == 5:
        rank = row['market_cap_rank_in_sector']
        v = 'LEADER' if (rank is not None and rank <= 5) else 'GEM'
    else:
        v = 'WATCH'
    print(f'  VERDICT = {v}')


def main():
    rows = load_rows()
    index = {r['symbol']: r for r in rows}

    print('=' * 80)
    print(f'  Phase 1.9 anchor test-by-test breakdown @ T={T_LOCKED*100:.0f}%')
    print('=' * 80)

    print('\n╔══ MATURE ANCHORS (expected: MATURE) ═══════════════════════════')
    for s in ANCHOR_MATURE:
        report_anchor(s, index.get(s))

    print('\n╔══ GROWTH ANCHORS (expected: GROWTH) ════════════════════════════')
    for s in ANCHOR_GROWTH:
        report_anchor(s, index.get(s))

    print('\n╔══ BORDERLINE ANCHORS ═══════════════════════════════════════════')
    for s in ANCHOR_BORDERLINE:
        report_anchor(s, index.get(s))

    # Summary: which anchors AVOID + why
    print('\n' + '=' * 80)
    print('  AVOID anchors — decomposition by reason')
    print('=' * 80)
    avoids = {'MATURE': [], 'GROWTH': []}
    for s in ALL_ANCHORS:
        row = index.get(s)
        if row is None:
            continue
        arch = classify(row, T_LOCKED)
        if arch == 'UNKNOWN':
            continue
        tests = MATURE_TESTS if arch == 'MATURE' else GROWTH_TESTS
        db_iter = mature_dealbreakers if arch == 'MATURE' else growth_dealbreakers
        results = [fn(row)[0] for _, fn in tests]
        passes = sum(1 for r in results if r is True)
        known = sum(1 for r in results if r is not None)
        fired = [name for name, f, _ in db_iter(row) if f]
        if known < 3:
            continue
        if fired:
            avoids[arch].append((s, 'dealbreaker:' + ','.join(fired)))
        elif passes <= 2:
            avoids[arch].append((s, f'{passes}/{known} tests'))

    for arch in ('MATURE', 'GROWTH'):
        if avoids[arch]:
            print(f'\n  {arch}:')
            for s, reason in avoids[arch]:
                print(f'    {s:6s}  {reason}')


if __name__ == '__main__':
    main()
