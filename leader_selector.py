#!/usr/bin/env python3
"""
Phase 1.4 — Leader Selector (Phase 1.9 4-verdict schema).

Reads screener_results.csv (Phase 1.3 output) and selects up to N tickers
for leaders.csv per the simplified Phase 1.9 rule:

    leaders.csv = all LEADER ∪ top GEM by good_firm_score until total = N

This replaces the pre-1.9 selection logic (3-step INDUSTRY_LEADER +
POTENTIAL_LEADER + per-sector HIDDEN_GEM with IL quality gate). The
Phase 1.9 archetype-dispatched `_verdict()` in `fundamental_screener.py`
already enforces:

  - LEADER = 5/5 archetype-tuned tests AND market_cap_rank_in_sector ≤ 5
             AND no dealbreaker
  - GEM    = 5/5 archetype-tuned tests AND market_cap_rank_in_sector > 5
             AND no dealbreaker
  - WATCH  = 3-4/5 tests, no dealbreaker
  - AVOID  = ≤2/5 tests OR any dealbreaker

So the pre-1.9 IL quality gate (revenue_3y_cagr ≥ 0 + OM ≥ 5% + R40 ≥ 20)
is redundant — LEADER already means "passes all 5 archetype-tuned tests
for either MATURE or GROWTH, with proven sector-rank standing." The old
Step 3 per-SIC-2 sector diversifier also goes away: LEADER ∪ top-GEM by
score already reflects the business-quality ordering we want, and GEM
is itself the "best-of-sector runner-up" tier in the new schema.

Dealbreaker screen kept as defense-in-depth (should be a no-op since
LEADER/GEM verdicts already exclude rows with any flag_* set).

Output: leaders.csv with symbol, cik, name, sector, sic, verdict,
good_firm_score, archetype, market_cap, selection_reason. Consumed by
Layer 2's get_all_symbols() — 'symbol' is the first column so a plain
DictReader works.

Typical usage:
    python leader_selector.py --build

Advanced:
    python leader_selector.py --build --target-size 150
    python leader_selector.py --build --screener custom_screen.csv --out my_leaders.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCREENER_CSV = HERE / "screener_results.csv"
UNIVERSE_CSV = HERE / "universe_raw.csv"  # source of truth for symbol → cik
LEADERS_CSV = HERE / "leaders.csv"

DEFAULT_TARGET_SIZE = 100

LEADERS_FIELDS = [
    'symbol', 'cik', 'name', 'sector', 'sic',
    'verdict', 'good_firm_score', 'archetype',
    'market_cap', 'selection_reason',
]

# Defense-in-depth: Phase 1.9 `_verdict()` already routes rows with any
# dealbreaker flag straight to AVOID, so a row reaching this selector
# tagged LEADER or GEM should never trip these checks. Kept anyway so a
# bug in the screener can't silently leak a flagged ticker into
# leaders.csv (which feeds Layer 2's daily prediction pipeline).
DEALBREAKER_FIELDS = ('flag_diluting', 'flag_burning_cash',
                      'flag_spac_or_microcap')


def _is_dealbreaker(row):
    """True if any dealbreaker flag is set. CSV stores bools as '0'/'1'."""
    for f in DEALBREAKER_FIELDS:
        v = row.get(f, '')
        if isinstance(v, bool):
            if v:
                return True
        elif isinstance(v, (int, float)):
            if v:
                return True
        elif isinstance(v, str):
            if v.strip().lower() in ('1', 'true', 'yes'):
                return True
    return False


def _float(v, default=0.0):
    """Safe float coerce for sorting — never raises, treats blanks as default."""
    if v is None or v == '' or v == 'None':
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _load_screener(path):
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Screener CSV not found: {path}")
    with path.open('r', newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _load_cik_map(path):
    """
    Build a symbol → cik lookup from universe_raw.csv (Phase 1.0 output).
    Returns an empty dict if the file doesn't exist — caller warns.
    """
    mapping = {}
    path = Path(path)
    if not path.exists():
        return mapping
    with path.open('r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            sym = (r.get('symbol') or '').strip().upper()
            cik = (r.get('cik') or '').strip()
            if sym and cik:
                mapping[sym] = cik
    return mapping


def select_leaders(screener_rows, target_size=DEFAULT_TARGET_SIZE):
    """
    Phase 1.9 selection: all LEADER ∪ top GEM by good_firm_score until
    total = target_size.

    Returns (selected_rows, selection_reason_map).

    selected_rows preserves screener_rows' row objects verbatim; the caller
    is responsible for projecting down to LEADERS_FIELDS on write.

    selection_reason_map: symbol → short string ('leader' or 'gem').

    Ordering notes:
      - LEADER rows come first, sorted by good_firm_score desc (tie-break
        market_cap desc). If LEADER count ≥ target_size we truncate there
        and emit zero GEMs — this is intentional: a target_size=100 run
        with 150 LEADERs means "top 100 LEADERs" without GEM dilution.
      - GEMs fill whatever slack remains, same sort key.
      - WATCH / AVOID / INSUFFICIENT_DATA / UNKNOWN never enter the pool.
    """
    # Pre-filter: exclude everything except LEADER + GEM, and defensively
    # drop any row carrying a dealbreaker flag (should be impossible for
    # LEADER/GEM under Phase 1.9 but we belt-and-brace).
    pool_leader = []
    pool_gem = []
    for r in screener_rows:
        verdict = (r.get('verdict') or '').upper()
        if verdict not in ('LEADER', 'GEM'):
            continue
        if _is_dealbreaker(r):
            continue
        if verdict == 'LEADER':
            pool_leader.append(r)
        else:
            pool_gem.append(r)

    def _sort_key(r):
        # Primary: score desc. Secondary: mcap desc. Stable, reproducible.
        return (-_float(r.get('good_firm_score')),
                -_float(r.get('market_cap')))

    pool_leader.sort(key=_sort_key)
    pool_gem.sort(key=_sort_key)

    selected = []
    reasons = {}

    def _add(row, reason):
        sym = (row.get('symbol') or '').strip().upper()
        if not sym or sym in reasons:
            return False
        selected.append(row)
        reasons[sym] = reason
        return True

    # Step 1: all LEADER rows (capped at target_size).
    for r in pool_leader:
        if len(selected) >= target_size:
            break
        _add(r, 'leader')

    # Step 2: top GEM by score to fill remaining slots.
    if len(selected) < target_size:
        for r in pool_gem:
            if len(selected) >= target_size:
                break
            _add(r, 'gem')

    return selected, reasons


def write_leaders_csv(selected, reasons, cik_map, path):
    path = Path(path)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=LEADERS_FIELDS)
        writer.writeheader()
        for r in selected:
            sym = (r.get('symbol') or '').strip().upper()
            writer.writerow({
                'symbol': sym,
                'cik': cik_map.get(sym, ''),
                'name': r.get('name', '') or '',
                'sector': r.get('sector', '') or '',
                'sic': r.get('sic', '') or '',
                'verdict': r.get('verdict', '') or '',
                'good_firm_score': r.get('good_firm_score', '') or '',
                'archetype': r.get('archetype', '') or '',
                'market_cap': r.get('market_cap', '') or '',
                'selection_reason': reasons.get(sym, ''),
            })
    print(f"Wrote {len(selected)} leaders to {path}")


def main():
    p = argparse.ArgumentParser(
        description="Phase 1.4 — Select up to N industry leaders from "
                    "screener results (Phase 1.9 LEADER ∪ top-GEM schema)"
    )
    p.add_argument('--screener', default=str(SCREENER_CSV),
                   help=f'Input screener_results.csv '
                        f'(default: {SCREENER_CSV.name})')
    p.add_argument('--universe', default=str(UNIVERSE_CSV),
                   help=f'universe_raw.csv for CIK lookup '
                        f'(default: {UNIVERSE_CSV.name})')
    p.add_argument('--out', default=str(LEADERS_CSV),
                   help=f'Output leaders.csv (default: {LEADERS_CSV.name})')
    p.add_argument('--target-size', type=int, default=DEFAULT_TARGET_SIZE,
                   help=f'Cap on leaders (default: {DEFAULT_TARGET_SIZE})')
    p.add_argument('--build', action='store_true',
                   help='Run the selection pipeline '
                        '(required — otherwise prints help)')
    args = p.parse_args()

    if not args.build:
        print("Nothing to do. Pass --build to run the selection.")
        print("Tip: python leader_selector.py --build")
        return

    screener_rows = _load_screener(args.screener)
    cik_map = _load_cik_map(args.universe)

    # Pre-selection stats (verdict distribution)
    by_verdict = {}
    for r in screener_rows:
        v = (r.get('verdict') or '').upper() or 'UNKNOWN'
        by_verdict[v] = by_verdict.get(v, 0) + 1
    print(f"Input: {len(screener_rows)} tickers from {args.screener}")
    for v in sorted(by_verdict.keys()):
        print(f"  {v}: {by_verdict[v]}")
    if not cik_map:
        print(f"[warn] No CIK map from {args.universe}; "
              f"'cik' column will be empty.")
    else:
        print(f"Loaded {len(cik_map)} symbol → cik mappings "
              f"from {args.universe}")

    selected, reasons = select_leaders(screener_rows,
                                       target_size=args.target_size)

    # Post-selection breakdown by reason (leader vs gem)
    by_reason = {}
    for sym, reason in reasons.items():
        by_reason[reason] = by_reason.get(reason, 0) + 1
    print(f"\nSelected: {len(selected)} leaders (cap={args.target_size})")
    for k in sorted(by_reason.keys()):
        print(f"  {k}: {by_reason[k]}")

    # Archetype split — quick eyeball for MATURE/GROWTH balance
    by_arch = {}
    for r in selected:
        a = (r.get('archetype') or 'UNKNOWN').upper()
        by_arch[a] = by_arch.get(a, 0) + 1
    if by_arch:
        print("\nSelected by archetype:")
        for a in sorted(by_arch.keys()):
            print(f"  {a}: {by_arch[a]}")

    # Any LEADER rows dropped by dealbreaker defense (should be empty —
    # Phase 1.9 verdict already excludes those)
    all_leader_syms = {(r.get('symbol') or '').strip().upper()
                       for r in screener_rows
                       if (r.get('verdict') or '').upper() == 'LEADER'}
    selected_syms = set(reasons.keys())
    leader_dropped = all_leader_syms - selected_syms
    # Only warn if the cause is the dealbreaker screen (not target_size cap)
    leader_count_in_selection = sum(1 for v in reasons.values()
                                    if v == 'leader')
    if leader_dropped and leader_count_in_selection == len(all_leader_syms):
        print(f"\n[warn] LEADER verdicts dropped by dealbreaker screen "
              f"(unexpected under Phase 1.9): {sorted(leader_dropped)}")

    write_leaders_csv(selected, reasons, cik_map, args.out)


if __name__ == "__main__":
    main()
