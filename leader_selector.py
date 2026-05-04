#!/usr/bin/env python3
"""
Phase 1.4 — Leader Selector (Round 9a 4-verdict schema, size-blind).

Reads screener_results.csv (Phase 1.3 output) and selects up to N tickers
for leaders.csv:

    leaders.csv = top-N LEADER rows by good_firm_score
                  (under-fill if fewer than N LEADERs exist)

Round 9a (2026-05-03) collapsed the LEADER/GEM split — see
`fundamental_screener._verdict`. The pool is now a single LEADER bucket
sorted by `good_firm_score` desc (tie-break: market_cap desc). If LEADER
count < target_size we accept under-fill rather than reaching into WATCH:
WATCH means "3–4/5 tests passed", which is a different quality tier than
the rest of `leaders.csv` and would dilute Layer 2's training pool.

Pre-Round-9a history: the schema split 5/5 winners into LEADER (top-5
sector-rank) and GEM (everything else), and this selector filled with
`all LEADER ∪ top GEM by good_firm_score`. With GEM gone, the union step
is gone too. The pre-1.9 selection (3-step INDUSTRY_LEADER + POTENTIAL_LEADER
+ per-sector HIDDEN_GEM with IL quality gate) was already retired in 1.9
and stays retired.

Dealbreaker screen kept as defense-in-depth (should be a no-op since the
LEADER verdict already excludes rows with any flag_* set).

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

# Defense-in-depth: `_verdict()` already routes rows with any dealbreaker
# flag straight to AVOID, so a row reaching this selector tagged LEADER
# should never trip these checks. Kept anyway so a bug in the screener
# can't silently leak a flagged ticker into leaders.csv (which feeds
# Layer 2's daily prediction pipeline).
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


def _has_active_forensic_flags(row):
    """True if `forensic_flag_count` > 0 on this row.

    Round May 15: forensic flags (ni_ocf_divergence, leverage_high,
    going_concern, dilution_velocity) ride alongside the verdict as a
    SEPARATE quality signal — the verdict (LEADER/WATCH/AVOID/INSUFFICIENT_DATA)
    encodes pure quality (Round 9a invariant), but the leaders pool excludes
    any row carrying an unsuppressed forensic flag so Layer 2's training
    set doesn't get polluted with rows that have a fatal-flaw signal the
    rubric tests don't capture.

    `forensic_flag_count` is computed once in the screener AFTER override
    application — single source of truth per PATTERNS.md P-4. We read it
    here without re-computing or re-applying overrides.

    Empty/blank cell on legacy rows from pre-Round-May-15 screener_results.csv
    parses as 0 — graceful degradation to the previous (forensic-flag-blind)
    behaviour rather than dropping every row.
    """
    raw = row.get('forensic_flag_count', '')
    if raw is None or raw == '':
        return False
    try:
        return int(float(raw)) > 0
    except (TypeError, ValueError):
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
    Round 9a selection: top-N LEADER rows by good_firm_score.

    Returns (selected_rows, selection_reason_map).

    selected_rows preserves screener_rows' row objects verbatim; the caller
    is responsible for projecting down to LEADERS_FIELDS on write.

    selection_reason_map: symbol → 'leader' (only value emitted).

    Ordering: LEADER rows sorted by good_firm_score desc, tie-break
    market_cap desc. Stable across runs given identical input.

    Under-fill is acceptable: if fewer than target_size LEADERs exist we
    do not reach into WATCH. WATCH means 3–4/5 tests passed (not a quality
    peak) and mixing it into leaders.csv would dilute Layer 2's training
    pool. Pre-Round-9a behaviour reached into GEM (5/5 + smaller-cap) for
    fill, but GEM no longer exists — quality-equivalent rows are now all
    tagged LEADER directly.
    """
    # Pre-filter: LEADER only, defensively drop any row carrying a
    # dealbreaker flag (should be impossible under the current verdict
    # but we belt-and-brace), and drop any row with an active forensic
    # flag (Round May 15 — see _has_active_forensic_flags). Forensic-
    # flag drops are NOT defensive defence-in-depth: the verdict layer
    # deliberately leaves forensic flags out so quality and forensic
    # concerns stay separable on the row, and only the LEADER pool
    # applies the filter.
    pool_leader = []
    forensic_dropped = []
    for r in screener_rows:
        verdict = (r.get('verdict') or '').upper()
        if verdict != 'LEADER':
            continue
        if _is_dealbreaker(r):
            continue
        if _has_active_forensic_flags(r):
            forensic_dropped.append((r.get('symbol') or '').upper())
            continue
        pool_leader.append(r)
    # Stash the forensic-drop list on the function so main() can surface
    # it in the under-fill log. Module-level state would couple the test
    # harness; a closure-style attach keeps it scoped.
    select_leaders._last_forensic_dropped = forensic_dropped

    def _sort_key(r):
        # Primary: score desc. Secondary: mcap desc. Stable, reproducible.
        return (-_float(r.get('good_firm_score')),
                -_float(r.get('market_cap')))

    pool_leader.sort(key=_sort_key)

    selected = []
    reasons = {}

    def _add(row, reason):
        sym = (row.get('symbol') or '').strip().upper()
        if not sym or sym in reasons:
            return False
        selected.append(row)
        reasons[sym] = reason
        return True

    for r in pool_leader:
        if len(selected) >= target_size:
            break
        _add(r, 'leader')

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
                    "screener results (Round 9a top-N LEADER schema)"
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

    # Round 9a: surface under-fill explicitly. WATCH no longer feeds into
    # leaders.csv — if LEADER count is consistently below the cap on real
    # universe runs, that's a signal to revisit the rubric or the cap.
    # Round May 15: forensic-flag drops also reduce the eligible pool;
    # log them separately so a low fill caused by forensic flags vs.
    # genuinely-too-few-LEADERs is distinguishable in the rebuild log.
    forensic_dropped = getattr(select_leaders, '_last_forensic_dropped', [])
    if len(selected) < args.target_size:
        print(f"\n[note] Under-fill: {len(selected)}/{args.target_size} "
              f"slots filled. Pool exhausted at the LEADER tier (WATCH "
              f"intentionally not eligible).")
    if forensic_dropped:
        print(f"[note] {len(forensic_dropped)} LEADER row(s) dropped by "
              f"forensic-flag screen (forensic_flag_count > 0): "
              f"{sorted(forensic_dropped)}")

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
    # the verdict already routes flagged rows to AVOID)
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
              f"(unexpected — verdict should pre-filter these): "
              f"{sorted(leader_dropped)}")

    write_leaders_csv(selected, reasons, cik_map, args.out)


if __name__ == "__main__":
    main()
