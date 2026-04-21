"""Phase 1.9 diagnostic — why is CRWD revenue_ttm = None?

CrowdStrike shows in screener_results.csv as INSUFFICIENT_DATA with:
  - revenue_ttm / yoy / 3y_cagr / gross_margin / op_margin / fcf_margin  → all blank
  - operating_cash_flow_ttm = 1.61B, free_cash_flow_ttm = 1.31B, roic = -14%  → present

That means Revenue is the single failing metric. ROIC computes (Assets +
OperatingIncomeLoss both ingested), so CRWD's overall ingestion succeeded;
it's a Revenue-tag-specific issue. Four likely causes:

  H1. No Revenue facts at all (none of the 4 tags in the chain are filed)
  H2. Facts exist but stored as 'instant' (start_date missing → bad ingest)
  H3. Facts exist as 'duration' but _classify_period() labels NONE as 'annual'
      (CRWD's Feb-1 to Jan-31 fiscal year windows fall outside 330-400 days?)
  H4. Facts exist + classify correctly but get_facts() ORDER BY drops them

This dumps the data to tell them apart. Usage:
    python diag_crwd_revenue.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
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


def _days(start, end):
    try:
        s = datetime.strptime((start or '')[:10], "%Y-%m-%d").date()
        e = datetime.strptime((end or '')[:10], "%Y-%m-%d").date()
        return (e - s).days
    except Exception:
        return None


def _classify(days):
    if days is None:
        return 'inst?'
    if 330 <= days <= 400:
        return 'annual'
    if 260 <= days <= 290:
        return 'ytd_9mo'
    if 170 <= days <= 200:
        return 'ytd_6mo'
    if 80 <= days <= 100:
        return 'quarter'
    return f'other({days}d)'


def main():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        raise SystemExit(1)
    conn = sqlite3.connect(str(DB_PATH))

    # (1) Ticker table — did ingest succeed at all?
    print('=' * 80)
    print("  (1) CRWD ingestion status (tickers table)")
    print('=' * 80)
    rows = conn.execute(
        "SELECT symbol, cik, name, sic, sic_description, last_fetched, status, error "
        "FROM tickers WHERE symbol='CRWD'"
    ).fetchall()
    if not rows:
        print("  (CRWD NOT IN tickers TABLE — ingest never ran)")
    else:
        for r in rows:
            print(f"  symbol   = {r[0]}")
            print(f"  cik      = {r[1]}")
            print(f"  name     = {r[2]}")
            print(f"  sic      = {r[3]}  ({r[4]})")
            print(f"  fetched  = {r[5]}")
            print(f"  status   = {r[6]}")
            print(f"  error    = {r[7]}")

    # (2) What metrics got ingested for CRWD?
    print()
    print('=' * 80)
    print("  (2) CRWD — all metrics ingested")
    print('=' * 80)
    rows = conn.execute(
        """SELECT metric, COUNT(*) n, MIN(end_date), MAX(end_date)
             FROM facts WHERE symbol='CRWD'
         GROUP BY metric ORDER BY metric"""
    ).fetchall()
    if not rows:
        print("  (NO FACTS AT ALL — ingest empty)")
    else:
        print(f"  {'metric':40s}  {'n':>4s}  {'d_min':10s}  {'d_max':10s}")
        for m, n, d_min, d_max in rows:
            print(f"  {m:40s}  {n:>4d}  {d_min!s:10s}  {d_max!s:10s}")

    # (3) Revenue specifically — tag census
    print()
    print('=' * 80)
    print("  (3) CRWD Revenue — xbrl_tag × period_type census")
    print('=' * 80)
    rows = conn.execute(
        """SELECT xbrl_tag, period_type, COUNT(*) n,
                  MIN(end_date) d_min, MAX(end_date) d_max
             FROM facts WHERE symbol='CRWD' AND metric='Revenue'
         GROUP BY xbrl_tag, period_type
         ORDER BY n DESC"""
    ).fetchall()
    if not rows:
        print("  (NO Revenue FACTS — tag chain missed CRWD's filings entirely)")
        print("  → check SEC companyfacts endpoint directly for tags we don't walk")
    else:
        for tag, ptype, n, d_min, d_max in rows:
            print(f"  tag={tag!s:60s}  type={ptype!s:9s}  "
                  f"n={n:3d}  range={d_min}..{d_max}")

    # (4) Every Revenue duration row — with days + classify verdict
    print()
    print('=' * 80)
    print("  (4) CRWD Revenue — all duration facts "
          "(days + classify + what latest_ttm would pick)")
    print('=' * 80)
    rows = conn.execute(
        """SELECT fiscal_year, fiscal_period, start_date, end_date, form,
                  value, xbrl_tag, accession
             FROM facts
            WHERE symbol='CRWD' AND metric='Revenue'
              AND period_type='duration'
         ORDER BY end_date DESC, accession DESC"""
    ).fetchall()
    if not rows:
        print("  (no duration Revenue facts)")
    else:
        print(f"  {'fy':>4s} {'fp':>3s} {'start':10s} {'end':10s} "
              f"{'form':>4s} {'days':>4s} {'period':>10s} "
              f"{'value':>14s}  {'tag':40s}")
        annual_count = 0
        for fy, fp, s, e, form, v, tag, acc in rows:
            d = _days(s, e)
            cls = _classify(d)
            if cls == 'annual':
                annual_count += 1
            print(f"  {fy!s:>4s} {fp!s:>3s} {s!s:10s} {e!s:10s} "
                  f"{form!s:>4s} {d!s:>4s} {cls:>10s} "
                  f"{_fmt(v):>14s}  {tag[:40]}")
        print()
        print(f"  >>> duration rows: {len(rows)}, "
              f"rows labeled 'annual' (330–400 days): {annual_count}")
        print( "  >>> latest_ttm_fact picks the first 'annual' row; "
               "if zero, falls through to FY+YTD rollover, then 4-quarter sum.")

    # (5) Final: what does the actual function return?
    print()
    print('=' * 80)
    print("  (5) Live call: latest_ttm('CRWD', 'Revenue') returns what?")
    print('=' * 80)
    try:
        import sys
        sys.path.insert(0, str(DB_PATH.parent))
        from fundamental_metrics import latest_ttm_fact, ttm_at_offset
        f = latest_ttm_fact('CRWD', 'Revenue', conn=conn)
        print(f"  latest_ttm_fact → {f}")
        prior = ttm_at_offset('CRWD', 'Revenue', 4, conn=conn)
        print(f"  ttm_at_offset(quarters_back=4) → {_fmt(prior)}")
    except Exception as exc:
        print(f"  (import or call failed: {type(exc).__name__}: {exc})")

    conn.close()


if __name__ == '__main__':
    main()
