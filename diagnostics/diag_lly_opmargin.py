"""Phase 1.7i diagnostic — why is LLY operating_margin_ttm=None?

Dumps every OperatingIncomeLoss-family fact for LLY and the closest
XBRL siblings that could serve as a fallback.

Usage:
    python diag_lly_opmargin.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / 'fundamentals.db'


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


def main():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        raise SystemExit(1)
    conn = sqlite3.connect(str(DB_PATH))

    # (1) What metrics does the ingester have for LLY at all?
    print('=' * 80)
    print("  (1) LLY — what metrics did we ingest?")
    print('=' * 80)
    rows = conn.execute(
        """
        SELECT metric, COUNT(*) n, MIN(end_date), MAX(end_date)
          FROM facts
         WHERE symbol='LLY'
      GROUP BY metric
      ORDER BY metric
        """
    ).fetchall()
    if not rows:
        print("  (NO FACTS AT ALL — LLY not ingested?)")
    else:
        print(f"  {'metric':40s}  {'n':>4s}  {'d_min':10s}  {'d_max':10s}")
        for m, n, d_min, d_max in rows:
            print(f"  {m:40s}  {n:>4d}  {d_min!s:10s}  {d_max!s:10s}")

    # (2) Specifically OperatingIncomeLoss — what xbrl_tags ingested?
    print()
    print('=' * 80)
    print("  (2) LLY OperatingIncomeLoss — xbrl_tag census")
    print('=' * 80)
    rows = conn.execute(
        """
        SELECT xbrl_tag, period_type, COUNT(*) n,
               MIN(end_date) d_min, MAX(end_date) d_max
          FROM facts
         WHERE symbol='LLY' AND metric='OperatingIncomeLoss'
      GROUP BY xbrl_tag, period_type
      ORDER BY n DESC
        """
    ).fetchall()
    if not rows:
        print("  (NO OperatingIncomeLoss FACTS — need to add tag-chain fallback)")
    else:
        for tag, ptype, n, d_min, d_max in rows:
            print(f"  tag={tag:60s}  type={ptype:8s}  n={n:3d}  "
                  f"range={d_min}..{d_max}")

    # (3) Latest 10 TTM-relevant OperatingIncomeLoss rows
    print()
    print('=' * 80)
    print("  (3) LLY OperatingIncomeLoss — latest 10 rows "
          "(what latest_ttm walks)")
    print('=' * 80)
    rows = conn.execute(
        """
        SELECT fiscal_year, fiscal_period, start_date, end_date, form,
               value, xbrl_tag, accession
          FROM facts
         WHERE symbol='LLY' AND metric='OperatingIncomeLoss'
      ORDER BY end_date DESC, accession DESC
         LIMIT 10
        """
    ).fetchall()
    if not rows:
        print("  (no rows)")
    else:
        print(f"  {'fy':>4s}  {'fp':>3s}  {'start':10s}  {'end':10s}  "
              f"{'form':>4s}  {'value':>14s}  {'xbrl_tag':40s}  accession")
        for fy, fp, s, e, form, v, tag, acc in rows:
            print(f"  {fy!s:>4s}  {fp!s:>3s}  {s!s:10s}  {e!s:10s}  "
                  f"{form!s:>4s}  {_fmt(v):>14s}  {tag:40s}  {acc}")

    # (4) Sibling tags that could be a fallback — same "income" concept
    print()
    print('=' * 80)
    print("  (4) LLY — ALL 'income' / 'operating' XBRL tags ingested")
    print('=' * 80)
    rows = conn.execute(
        """
        SELECT xbrl_tag, metric, period_type, COUNT(*) n
          FROM facts
         WHERE symbol='LLY'
           AND (lower(xbrl_tag) LIKE '%operatingincome%'
                OR lower(xbrl_tag) LIKE '%incomeloss%')
      GROUP BY xbrl_tag, metric, period_type
      ORDER BY n DESC
        """
    ).fetchall()
    if not rows:
        print("  (no matching tags at all)")
    else:
        for tag, metric, ptype, n in rows:
            print(f"  tag={tag:70s}  metric={metric:30s}  "
                  f"type={ptype:8s}  n={n:3d}")

    # (5) Cross-check — do we see revenue for LLY? (if yes, ingest worked)
    print()
    print('=' * 80)
    print("  (5) LLY Revenue — sanity check (should have current data)")
    print('=' * 80)
    rows = conn.execute(
        """
        SELECT xbrl_tag, period_type, COUNT(*) n,
               MAX(end_date) d_max
          FROM facts
         WHERE symbol='LLY' AND metric='Revenue'
      GROUP BY xbrl_tag, period_type
      ORDER BY n DESC
        """
    ).fetchall()
    for tag, ptype, n, d_max in rows:
        print(f"  tag={tag:50s}  type={ptype:8s}  n={n:3d}  max_end={d_max}")

    conn.close()


if __name__ == '__main__':
    main()
