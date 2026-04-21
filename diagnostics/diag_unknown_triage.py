"""Phase 1.9b — triage every UNKNOWN/INSUFFICIENT_DATA ticker from
screener_results.csv by *why* they failed.

After the Phase 1.9 rubric lands, 38 of 1,430 scored rows came back as
archetype=UNKNOWN / verdict=INSUFFICIENT_DATA. The CRWD case surfaced
one failure mode (tag-chain missed `RevenueFromContractWithCustomer
IncludingAssessedTax`). The rest may share CRWD's bug, may be a
different data-quality issue, or may be filers that never belonged
in the prescreened pool (BDCs, brokers) — each implies a different fix.

This script reads screener_results.csv + fundamentals.db locally and
labels each UNKNOWN row with one of:

  CAT_A_TAG_MISS   — ingest captured 0 Revenue facts; operating biz fine
                     (has OCF); likely uses a us-gaap Revenue alias we
                     don't walk. Fix = add tag + target re-fetch.

  CAT_B_NO_PRIOR   — Revenue TTM computes (we can see gross_margin or
                     operating_margin), but ttm_at_offset(quarters_back=4)
                     returns None. Usually: insufficient FY history or
                     non-standard fiscal year. May be a natural UNKNOWN
                     (young filer).

  CAT_C_FINANCIAL  — SIC is 6211 (broker-dealer), 6726 (investment fund),
                     6770 (blank-check), or sector blank. Good Firm
                     Framework doesn't apply; fix = Phase 1.1 SIC
                     exclusion update.

  CAT_D_OTHER      — none of the above (inspect manually).

Usage:
    python diag_unknown_triage.py
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / 'fundamentals.db'
CSV_PATH = ROOT / 'screener_results.csv'

# SIC codes of filers the Good Firm Framework can't evaluate.
FINANCIAL_SIC = {
    '6211',  # Security Brokers, Dealers & Flotation Companies
    '6722',  # Investment Offices (already excluded at prescreen, listed here for completeness)
    '6726',  # Investment Offices, NEC — BDCs live here
    '6770',  # Blank Checks
    '6199',  # Finance Services (already excluded)
}


def _has(row, field):
    """True if CSV cell is non-empty and not just whitespace."""
    v = (row.get(field) or '').strip()
    return v != ''


def _revenue_fact_count(conn, symbol):
    """How many Revenue rows does this ticker have in the facts table?"""
    r = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE symbol=? AND metric='Revenue'",
        (symbol,)
    ).fetchone()
    return r[0] if r else 0


def _revenue_annual_count(conn, symbol):
    """How many ~annual-length duration Revenue rows?"""
    # 330-400 day window = 'annual' per _classify_period
    rows = conn.execute(
        """SELECT start_date, end_date FROM facts
             WHERE symbol=? AND metric='Revenue'
               AND period_type='duration'
               AND start_date IS NOT NULL""",
        (symbol,)
    ).fetchall()
    from datetime import datetime
    n = 0
    for s, e in rows:
        try:
            sd = datetime.strptime(s[:10], "%Y-%m-%d").date()
            ed = datetime.strptime(e[:10], "%Y-%m-%d").date()
            if 330 <= (ed - sd).days <= 400:
                n += 1
        except Exception:
            continue
    return n


def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found")
        raise SystemExit(1)
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        raise SystemExit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    with CSV_PATH.open(encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    unknown = [r for r in rows
               if (r.get('archetype') or '').upper() == 'UNKNOWN']
    print(f"Total scored rows: {len(rows)}")
    print(f"UNKNOWN / INSUFFICIENT_DATA rows: {len(unknown)}")
    print()

    buckets = {
        'CAT_A_TAG_MISS':  [],
        'CAT_B_NO_PRIOR':  [],
        'CAT_C_FINANCIAL': [],
        'CAT_D_OTHER':     [],
    }

    for r in unknown:
        sym = r.get('symbol') or ''
        sic = (r.get('sic') or '').strip()
        sector = (r.get('sector') or '').strip()
        rev_fact_n = _revenue_fact_count(conn, sym)
        rev_ann_n = _revenue_annual_count(conn, sym)

        gm_present = _has(r, 'gross_margin_ttm')
        om_present = _has(r, 'operating_margin_ttm')
        fcfm_present = _has(r, 'fcf_margin_ttm')
        ocf_present = _has(r, 'operating_cash_flow_ttm')
        has_rev_ttm = gm_present or om_present or fcfm_present

        # Classification priority: financial first (since those shouldn't
        # be in prescreen at all), then tag-miss, then no-prior, else other.
        if sic in FINANCIAL_SIC or sector == '':
            cat = 'CAT_C_FINANCIAL'
        elif rev_fact_n == 0 and ocf_present:
            cat = 'CAT_A_TAG_MISS'
        elif has_rev_ttm and rev_ann_n >= 1:
            cat = 'CAT_B_NO_PRIOR'
        else:
            cat = 'CAT_D_OTHER'

        buckets[cat].append({
            'symbol': sym,
            'sic': sic,
            'sector': sector[:40],
            'rev_fact_n': rev_fact_n,
            'rev_ann_n': rev_ann_n,
            'gm': gm_present,
            'om': om_present,
            'ocf': ocf_present,
        })

    # Print each bucket
    for cat, lst in buckets.items():
        print('=' * 80)
        print(f"  {cat}  (n={len(lst)})")
        print('=' * 80)
        if not lst:
            print("  (none)")
            print()
            continue
        print(f"  {'sym':8s} {'sic':>5s}  {'rev_n':>5s} {'ann_n':>5s}  "
              f"{'GM':>3s} {'OM':>3s} {'OCF':>4s}  sector")
        for row in lst:
            print(
                f"  {row['symbol']:8s} {row['sic']:>5s}  "
                f"{row['rev_fact_n']:>5d} {row['rev_ann_n']:>5d}  "
                f"{str(row['gm'])[0]:>3s} {str(row['om'])[0]:>3s} "
                f"{str(row['ocf'])[0]:>4s}  {row['sector']}"
            )
        print()

    # Summary
    print('=' * 80)
    print("  Summary / next actions")
    print('=' * 80)
    print(f"  CAT_A_TAG_MISS   {len(buckets['CAT_A_TAG_MISS']):3d}  "
          f"→ re-fetch these after verifying tag chain; examine SEC "
          f"companyfacts for any tag we're missing.")
    print(f"  CAT_B_NO_PRIOR   {len(buckets['CAT_B_NO_PRIOR']):3d}  "
          f"→ natural UNKNOWN (young filer or fiscal-year edge); accept as-is.")
    print(f"  CAT_C_FINANCIAL  {len(buckets['CAT_C_FINANCIAL']):3d}  "
          f"→ Phase 1.1 rule-C update: add SIC 6211 / 6726 / 6770 to "
          f"excluded_sic_ranges and re-run universe_builder --prescreen-only.")
    print(f"  CAT_D_OTHER      {len(buckets['CAT_D_OTHER']):3d}  "
          f"→ inspect manually (may need a new hypothesis).")

    conn.close()


if __name__ == '__main__':
    main()
