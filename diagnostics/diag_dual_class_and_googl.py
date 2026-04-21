"""Phase 1.7g/h diagnostic.

TWO investigations in one script:

(1) GOOGL `shares_growth_3y = null` — dump every WeightedAverageSharesBasic
    fact for GOOGL from `facts` table so we can see:
      * what xbrl_tag actually got ingested (Basic vs Diluted)
      * how many annual-duration FY rows exist (fy_series needs >= 4)
      * whether the 10:1 / dual-class segment tagging interferes
    Hypothesis: Alphabet's dual-class structure leads XBRL to report
    weighted-average shares per share-class (via dei:ClassOfStock member
    segments), and our ingest may miss segment-tagged facts.

(2) Duplicate-CIK scan of universe_prescreened.csv — every CIK that
    appears more than once (dual-class share tickers like GOOGL/GOOG,
    BRK-A/BRK-B, FWON.A/.B/.K, NWS/NWSA, FOX/FOXA, etc.). These are
    currently double-counted in:
      - HTTP fetch: edgar_fetcher hits same companyfacts endpoint twice
      - Sector ranking: fundamental_screener treats them as separate peers
      - Leader selection: both share classes compete for the ≤100 slots

Usage:
    python diag_dual_class_and_googl.py

Reads from: fundamentals.db, universe_prescreened.csv
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / 'fundamentals.db'
PRESCREEN_CSV = HERE / 'universe_prescreened.csv'


def _fmt(v):
    if v is None:
        return 'NULL'
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e9:
        return f"{v/1e9:,.3f}B"
    if abs(v) >= 1e6:
        return f"{v/1e6:,.1f}M"
    if abs(v) >= 1e3:
        return f"{v:,.0f}"
    return f"{v:,.4f}"


# ─── (1) GOOGL shares facts dump ─────────────────────────────────────────────

def dump_googl_shares(conn):
    print('=' * 80)
    print('  (1) GOOGL — WeightedAverageSharesBasic + SharesOutstanding facts')
    print('=' * 80)

    for metric in ('WeightedAverageSharesBasic', 'SharesOutstanding'):
        print(f'\n[{metric}]  tag census (what xbrl_tag did we ingest?):')
        rows = conn.execute(
            """
            SELECT xbrl_tag, period_type, COUNT(*) n,
                   MIN(end_date) d_min, MAX(end_date) d_max
              FROM facts
             WHERE symbol='GOOGL' AND metric=?
          GROUP BY xbrl_tag, period_type
          ORDER BY n DESC
            """,
            (metric,),
        ).fetchall()
        if not rows:
            print('  (no rows)')
            continue
        for tag, ptype, n, d_min, d_max in rows:
            print(f'  tag={tag:55s}  type={ptype:8s}  n={n:3d}  '
                  f'range={d_min}..{d_max}')

        # Latest 10 10-K FY annual facts — this is what fy_series reads
        print(f'\n[{metric}]  latest 10 "FY+10-K+annual-duration" rows '
              f'(what fy_series sees):')
        rows = conn.execute(
            """
            SELECT fiscal_year, start_date, end_date, form, fiscal_period,
                   value, xbrl_tag, accession
              FROM facts
             WHERE symbol='GOOGL' AND metric=?
               AND period_type='duration'
               AND form='10-K'
               AND fiscal_period='FY'
          ORDER BY end_date DESC, accession DESC
             LIMIT 10
            """,
            (metric,),
        ).fetchall()
        if not rows:
            print('  (no rows — fy_series will return [] → shares_growth_3y=None)')
        else:
            print(f'  {"fy":>4s}  {"start":10s}  {"end":10s}  '
                  f'{"form":>4s}  {"per":>3s}  {"value":>14s}  '
                  f'{"accession":25s}  tag')
            for fy, s, e, form, fp, v, tag, acc in rows:
                print(f'  {fy!s:>4s}  {s!s:10s}  {e!s:10s}  '
                      f'{form!s:>4s}  {fp!s:>3s}  {_fmt(v):>14s}  '
                      f'{acc!s:25s}  {tag}')

        # Distinct fiscal years with annual-duration FY rows
        distinct_fy = conn.execute(
            """
            SELECT COUNT(DISTINCT fiscal_year)
              FROM facts
             WHERE symbol='GOOGL' AND metric=?
               AND period_type='duration'
               AND form='10-K'
               AND fiscal_period='FY'
               AND fiscal_year IS NOT NULL
            """,
            (metric,),
        ).fetchone()[0]
        print(f'\n[{metric}]  distinct fiscal_years available: {distinct_fy}')
        print(f'  (fy_series needs >= 4 for shares_growth_3y; '
              f'>= 2 for basic growth)')


# ─── (2) Duplicate-CIK scan ──────────────────────────────────────────────────

def dump_dup_ciks():
    print()
    print('=' * 80)
    print('  (2) Duplicate CIKs in universe_prescreened.csv — dual-class tickers')
    print('=' * 80)

    if not PRESCREEN_CSV.exists():
        print(f'  ERROR: {PRESCREEN_CSV} not found')
        return

    by_cik = defaultdict(list)
    with PRESCREEN_CSV.open('r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            cik = (row.get('cik') or '').strip()
            if cik:
                by_cik[cik].append({
                    'symbol': row['symbol'],
                    'name': row.get('name', ''),
                    'mcap': float(row['market_cap']) if row.get('market_cap') else 0,
                    'adv': float(row['avg_dollar_volume_90d'])
                           if row.get('avg_dollar_volume_90d') else 0,
                    'rev': float(row['annual_revenue'])
                           if row.get('annual_revenue') else 0,
                    'sic': row.get('sic', ''),
                    'sic_desc': row.get('sic_description', ''),
                })

    dupes = {cik: rows for cik, rows in by_cik.items() if len(rows) > 1}
    print(f'\n  Total prescreened rows: '
          f'{sum(len(v) for v in by_cik.values())}')
    print(f'  Unique CIKs:            {len(by_cik)}')
    print(f'  Duplicate-CIK groups:   {len(dupes)}  '
          f'(dual / multi-class tickers)')
    print(f'  Extra rows to dedup:    '
          f'{sum(len(v) for v in dupes.values()) - len(dupes)}')

    if not dupes:
        print('\n  (no duplicate CIKs found — no action needed)')
        return

    print(f'\n  All {len(dupes)} groups (sorted by combined market_cap desc):')
    print(f'  {"CIK":>10s}  {"name":40s}  {"tickers (keep ← drop)":40s}  '
          f'{"mcap":>10s}  SIC')
    sorted_groups = sorted(
        dupes.items(),
        key=lambda kv: max(r['mcap'] for r in kv[1]),
        reverse=True,
    )
    for cik, rows in sorted_groups:
        # Pick the ticker with highest avg_dollar_volume_90d as "keep"
        rows_sorted = sorted(rows, key=lambda r: r['adv'], reverse=True)
        keep = rows_sorted[0]['symbol']
        drops = [r['symbol'] for r in rows_sorted[1:]]
        tickers_str = f'{keep} ← drop: {",".join(drops)}'
        max_mcap = max(r['mcap'] for r in rows)
        name = rows[0]['name'][:40]
        sic = rows[0]['sic']
        print(f'  {cik:>10s}  {name:40s}  {tickers_str:40s}  '
              f'{_fmt(max_mcap):>10s}  {sic}')

    # Show full detail for each group
    print(f'\n  Detail per group:')
    for cik, rows in sorted_groups:
        print(f'\n  CIK {cik} — {rows[0]["name"]}')
        rows_sorted = sorted(rows, key=lambda r: r['adv'], reverse=True)
        for i, r in enumerate(rows_sorted):
            marker = '  ← KEEP (higher ADV)' if i == 0 else '  ← drop'
            print(f'    {r["symbol"]:6s}  mcap={_fmt(r["mcap"]):>10s}  '
                  f'adv_90d={_fmt(r["adv"]):>10s}  '
                  f'rev={_fmt(r["rev"]):>10s}  SIC={r["sic"]}{marker}')


def main():
    if not DB_PATH.exists():
        print(f'ERROR: {DB_PATH} not found')
        raise SystemExit(1)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        dump_googl_shares(conn)
        dump_dup_ciks()
    finally:
        conn.close()


if __name__ == '__main__':
    main()
