"""Phase 1.9 gate check — evaluate anchor correctness and S&P 100 LEADER
count against the full screener_results.csv (1,414 rows).

This is the plan's Phase 1.9 exit criterion:
  - ≥15 S&P 100 members tagged LEADER
  - MATURE anchors earn LEADER or GEM
  - GROWTH anchors earn LEADER or GEM
  - "Obvious-wrong verdict" rate ≤ 5%

Why this diagnostic and not threshold-tuning first:
  Today's full-pool run landed GEM=111 (2x plan target) and WATCH=607
  (1.5x plan target). Before re-tuning thresholds against these numbers,
  we need to know whether the *named* leaders people would recognize
  land correctly. If KO, JNJ, NVDA, MSFT, AAPL, META, WMT, PG, MCD,
  COST, CRM, NOW all land LEADER/GEM, the rubric is doing its job even
  if population counts drift from pre-empirical forecasts.

Reads screener_results.csv. Pure local. ~1 sec.
"""
from __future__ import annotations

import csv
from pathlib import Path

CSV_PATH = Path(__file__).parent / 'screener_results.csv'

# ── Anchor lists (user-locked 2026-04-18 during Phase 1.9 classifier work) ──

# MATURE anchors — established cash-throwers with single-digit revenue
# growth. Should earn LEADER or GEM under MATURE rubric.
MATURE_ANCHORS = [
    'KO', 'JNJ', 'CVX', 'PG', 'WMT', 'XOM', 'VZ', 'HD', 'MMM', 'DIS',
    'F', 'GM', 'DUK', 'NEE', 'SO', 'MCD', 'PEP', 'T', 'IBM',
]

# GROWTH anchors — clearly-accelerating businesses. Should earn LEADER
# or GEM under GROWTH rubric.
GROWTH_ANCHORS = [
    'NOW', 'CRWD', 'SNOW', 'NVDA', 'CRM', 'DDOG', 'ZS', 'PANW',
    'PLTR', 'NFLX', 'TSLA', 'ANET',
]

# Borderline anchors — known to sit near the T=12% classifier boundary.
# Tabulated to see landing but not counted in correctness rate.
BORDERLINE_ANCHORS = ['MSFT', 'META', 'AAPL', 'AMZN', 'GOOGL', 'LLY']

# S&P 100 as of ~2025 (snapshot). Some tickers (GOOG, BRK-B) are
# dual-class secondaries dropped by Phase 1.7h Rule F — missing from
# screener_results.csv by design, flagged separately below.
SP100 = set("""
AAPL ABBV ABT ACN ADBE AIG AMD AMGN AMZN AVGO AXP BA BAC BK BKNG BLK BMY
BRK-B C CAT CHTR CL CMCSA COF COP COST CRM CSCO CVS CVX DE DHR DIS DUK
EMR F FDX GD GE GILD GM GOOG GOOGL GS HD HON IBM INTC INTU ISRG JNJ JPM
KHC KO LIN LLY LMT LOW MA MCD MDLZ MDT MET META MMM MO MRK MS MSFT NEE
NFLX NKE NVDA ORCL PEP PFE PG PLTR PM PYPL QCOM RTX SBUX SCHW SLB SO SPG
T TGT TMO TMUS TSLA TXN UNH UNP UPS USB V VZ WFC WMT XOM
""".split())

# Dual-class secondaries expected to be missing (Phase 1.7h Rule F drop)
KNOWN_DEDUP_DROPS = {'GOOG', 'BRK-A', 'BF-A', 'CWEN-A', 'FOX', 'FWONA',
                     'HEI-A', 'LBTYK', 'LEN-B', 'NWS', 'UA', 'UHAL',
                     'ZG', 'BELFA'}


def _load():
    with CSV_PATH.open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def _fmt_pct(v, digits=1):
    try:
        return f"{float(v)*100:>{digits+5}.{digits}f}%"
    except (TypeError, ValueError):
        return "    -  "


def _fmt_num(v, digits=1):
    try:
        return f"{float(v):>{digits+5}.{digits}f}"
    except (TypeError, ValueError):
        return "   -  "


def _print_anchor_row(r, expected_label):
    v = r.get('verdict') or '-'
    a = r.get('archetype') or '-'
    passes = r.get('passes') or '-'
    score = r.get('good_firm_score') or '-'
    yoy = _fmt_pct(r.get('revenue_yoy_growth'))
    gm = _fmt_pct(r.get('gross_margin_ttm'))
    roic = _fmt_pct(r.get('roic_ttm'))
    r40 = _fmt_num(r.get('rule_40_score'))
    good = v in ('LEADER', 'GEM')
    mark = '✓' if good else ('~' if v == 'WATCH' else '✗')
    print(f"    {mark} {r['symbol']:6s} "
          f"[{v:6s}] {a:7s} "
          f"pass={passes}/5  score={score:>3s}  "
          f"yoy={yoy}  gm={gm}  roic={roic}  r40={r40}")


def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found — run screener first")
        raise SystemExit(1)

    rows = _load()
    by_sym = {r['symbol']: r for r in rows}
    print(f"Loaded {len(rows)} rows from screener_results.csv")
    print()

    # ── (1) MATURE anchor landings ──────────────────────────────────────
    print('=' * 100)
    print("  (1) MATURE anchors — should be LEADER or GEM "
          "(honest AVOID for CYCLICAL / stressed ok)")
    print('=' * 100)
    mature_good = mature_total = 0
    mature_missing = []
    for sym in MATURE_ANCHORS:
        r = by_sym.get(sym)
        if not r:
            mature_missing.append(sym)
            continue
        _print_anchor_row(r, 'MATURE')
        mature_total += 1
        if r.get('verdict') in ('LEADER', 'GEM'):
            mature_good += 1
    if mature_missing:
        print(f"    (missing from results: {', '.join(mature_missing)})")
    rate = mature_good / mature_total if mature_total else 0
    print(f"  → MATURE anchor LEADER/GEM rate: "
          f"{mature_good}/{mature_total} = {rate:.0%}")
    print()

    # ── (2) GROWTH anchor landings ──────────────────────────────────────
    print('=' * 100)
    print("  (2) GROWTH anchors — should be LEADER or GEM "
          "(WATCH for real-moat issues like CRWD ROIC<0 ok)")
    print('=' * 100)
    growth_good = growth_total = 0
    growth_missing = []
    for sym in GROWTH_ANCHORS:
        r = by_sym.get(sym)
        if not r:
            growth_missing.append(sym)
            continue
        _print_anchor_row(r, 'GROWTH')
        growth_total += 1
        if r.get('verdict') in ('LEADER', 'GEM'):
            growth_good += 1
    if growth_missing:
        print(f"    (missing from results: {', '.join(growth_missing)})")
    rate = growth_good / growth_total if growth_total else 0
    print(f"  → GROWTH anchor LEADER/GEM rate: "
          f"{growth_good}/{growth_total} = {rate:.0%}")
    print()

    # ── (3) Borderline landings (12-15% YoY dead-band) ──────────────────
    print('=' * 100)
    print("  (3) BORDERLINE anchors (near T=12% classifier boundary)")
    print('=' * 100)
    for sym in BORDERLINE_ANCHORS:
        r = by_sym.get(sym)
        if not r:
            print(f"    ? {sym:6s} (not in results)")
            continue
        _print_anchor_row(r, 'BORDERLINE')
    print()

    # ── (4) S&P 100 gate: ≥15 LEADERs required ──────────────────────────
    print('=' * 100)
    print("  (4) S&P 100 gate — ≥15 LEADERs required for Phase 1.5 handoff")
    print('=' * 100)
    in_results = [s for s in SP100 if s in by_sym]
    missing = [s for s in SP100 if s not in by_sym]
    dedup_drops = [s for s in missing if s in KNOWN_DEDUP_DROPS]
    other_missing = [s for s in missing if s not in KNOWN_DEDUP_DROPS]

    leaders = [r for r in rows
               if r['symbol'] in SP100 and r.get('verdict') == 'LEADER']
    gems = [r for r in rows
            if r['symbol'] in SP100 and r.get('verdict') == 'GEM']
    watches = [r for r in rows
               if r['symbol'] in SP100 and r.get('verdict') == 'WATCH']
    avoids = [r for r in rows
              if r['symbol'] in SP100 and r.get('verdict') == 'AVOID']

    print(f"  S&P 100 members in screener_results.csv: "
          f"{len(in_results)}/{len(SP100)}")
    print(f"    LEADER: {len(leaders)}   "
          f"GEM: {len(gems)}   "
          f"WATCH: {len(watches)}   "
          f"AVOID: {len(avoids)}")
    if dedup_drops:
        print(f"  Expected dedup drops: {', '.join(sorted(dedup_drops))}")
    if other_missing:
        print(f"  Other missing (prescreen rejects?): "
              f"{', '.join(sorted(other_missing))}")
    print()
    print(f"  LEADER list ({len(leaders)}):")
    for r in sorted(leaders,
                    key=lambda r: -float(r.get('good_firm_score') or 0)):
        _print_anchor_row(r, 'SP100')
    print()
    print(f"  GEM list ({len(gems)}):")
    for r in sorted(gems,
                    key=lambda r: -float(r.get('good_firm_score') or 0))[:20]:
        _print_anchor_row(r, 'SP100')
    if len(gems) > 20:
        print(f"    ... and {len(gems) - 20} more")
    print()
    if avoids:
        print(f"  AVOID list ({len(avoids)}) — sanity-check these:")
        for r in avoids:
            _print_anchor_row(r, 'SP100')
    print()

    # ── (5) Gate verdict ────────────────────────────────────────────────
    print('=' * 100)
    print("  Gate verdict")
    print('=' * 100)
    gate_leader = len(leaders) >= 15
    gate_mature = mature_good / mature_total >= 0.70 if mature_total else False
    gate_growth = growth_good / growth_total >= 0.70 if growth_total else False
    print(f"  [{('✓' if gate_leader else '✗')}] "
          f"≥15 S&P 100 LEADERs: {len(leaders)}")
    print(f"  [{('✓' if gate_mature else '✗')}] "
          f"≥70% MATURE anchors LEADER/GEM: "
          f"{mature_good}/{mature_total} = "
          f"{(mature_good/mature_total*100 if mature_total else 0):.0f}%")
    print(f"  [{('✓' if gate_growth else '✗')}] "
          f"≥70% GROWTH anchors LEADER/GEM: "
          f"{growth_good}/{growth_total} = "
          f"{(growth_good/growth_total*100 if growth_total else 0):.0f}%")
    print()
    if gate_leader and gate_mature and gate_growth:
        print("  ✅ Phase 1.9 gate passes. Clear to update leader_selector.py "
              "and emit leaders.csv.")
    else:
        print("  ⚠ Phase 1.9 gate partial. Inspect failing category(ies) "
              "above before rubric tuning.")


if __name__ == '__main__':
    main()
