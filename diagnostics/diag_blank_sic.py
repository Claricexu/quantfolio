"""Phase 1.9b — inventory all blank-SIC rows in universe_prescreened.csv
to size the CAT_C BDC exclusion decision.

Context: SEC submissions endpoint genuinely returns blank sic for BDCs
(verified via diag_bdc_sic.py — ARCC/OBDC/MAIN all got sic='' while MSFT
got '7372'). Options (a)-(e) on the table; (e) is dead. Before picking
(c) alternate source vs (d) blank-SIC-excludes, need to know:

  - Total blank-SIC count in the prescreened pool
  - Are they all BDC-like (all-capital-corp names, financial-services)?
  - OR are there legitimate operating cos that would get false-rejected?

Output:
  - Total row count
  - Blank-SIC row count
  - Per-row: symbol, name, mcap, annual_revenue, exchange
  - Quick name-heuristic split: "capital/lending/BDC" vs "other"

Read-only, zero HTTP. Run in ~1 sec.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

CSV_PATH = Path(__file__).parent / 'universe_prescreened.csv'

# Rough BDC-name heuristic (case-insensitive): used only to flag rows
# that look operating-co-like so we can eyeball false-positive risk.
BDC_NAME = re.compile(
    r'capital corp|capital inc|capital llc|bdc|business development|'
    r'lending fund|capital finance|specialty lending',
    re.IGNORECASE,
)


def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found")
        return

    with CSV_PATH.open(encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    print(f"universe_prescreened.csv: {len(rows)} rows")

    blanks = [r for r in rows if not (r.get('sic') or '').strip()]
    bdc_like = [r for r in blanks if BDC_NAME.search(r.get('name') or '')]
    other = [r for r in blanks if r not in bdc_like]

    print(f"blank-SIC rows: {len(blanks)}")
    print(f"  BDC-pattern names: {len(bdc_like)}")
    print(f"  other (possible false-positive risk): {len(other)}")
    print()

    print('=' * 95)
    print(f"  (A) BDC-pattern blank-SIC rows (safe to exclude)")
    print('=' * 95)
    print(f"  {'symbol':8s}  {'name':55s}  {'mcap_B':>7s}  {'rev_B':>7s}  exch")
    for r in sorted(bdc_like, key=lambda r: -float(r.get('market_cap') or 0)):
        mcap = float(r.get('market_cap') or 0) / 1e9
        rev = float(r.get('annual_revenue') or 0) / 1e9
        name = (r.get('name') or '')[:55]
        print(f"  {r['symbol']:8s}  {name:55s}  "
              f"{mcap:>7.2f}  {rev:>7.2f}  {r.get('exchange', '')}")

    print()
    print('=' * 95)
    print(f"  (B) OTHER blank-SIC rows (manual review — would be false-positives")
    print(f"      under option (d) blank-SIC→exclude)")
    print('=' * 95)
    if not other:
        print("  (none — option (d) is fully safe for the current prescreened pool)")
    else:
        print(f"  {'symbol':8s}  {'name':55s}  {'mcap_B':>7s}  {'rev_B':>7s}  exch")
        for r in sorted(other, key=lambda r: -float(r.get('market_cap') or 0)):
            mcap = float(r.get('market_cap') or 0) / 1e9
            rev = float(r.get('annual_revenue') or 0) / 1e9
            name = (r.get('name') or '')[:55]
            print(f"  {r['symbol']:8s}  {name:55s}  "
                  f"{mcap:>7.2f}  {rev:>7.2f}  {r.get('exchange', '')}")

    print()
    print('=' * 95)
    print(f"  (C) Diagnosis")
    print('=' * 95)
    if not other:
        print(f"  All {len(blanks)} blank-SIC rows are BDC-pattern names.")
        print(f"  → Option (d) 'blank-SIC defaults to exclude' is safe.")
        print(f"  → Fix is pure Phase 1.1 (prescreen_rules.json + _apply_rules),")
        print(f"    zero HTTP, re-runnable in <1 min.")
    else:
        print(f"  {len(other)} blank-SIC row(s) don't match BDC patterns. Inspect them —")
        print(f"  option (d) blanket blank-SIC exclude would false-reject them.")
        print(f"  If any are legitimate operating cos, need option (c) alternate")
        print(f"  SIC source (yfinance sector fallback in Phase 1.0 Stage 1c).")


if __name__ == '__main__':
    main()
