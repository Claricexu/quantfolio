"""Track C diagnostic — dump relevant XBRL facts for COST and ORCL.

Goal: figure out whether the 1.7d (gross margin) and 1.7e (ORCL FCF) bugs are
math bugs (no re-fetch needed) or tag bugs (bundle into Phase 1.7f re-fetch).

Usage:
    python diag_track_c.py

Looks at: Revenue, CostOfRevenue, GrossProfit, OperatingIncomeLoss,
          OperatingCashFlow, CapEx facts from the `facts` table — grouped by
          canonical metric, xbrl_tag, period_type, fiscal_year/period.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / 'fundamentals.db'

SYMBOLS = ['COST', 'ORCL']
METRICS_DURATION = [
    'Revenue', 'CostOfRevenue', 'GrossProfit', 'OperatingIncomeLoss',
    'OperatingCashFlow', 'CapEx',
]


def _fmt(v):
    if v is None:
        return 'NULL'
    if abs(v) >= 1e9:
        return f"${v/1e9:,.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:,.1f}M"
    return f"${v:,.0f}"


def dump_symbol(conn, sym: str) -> None:
    print(f"\n{'=' * 78}")
    print(f"  {sym}")
    print('=' * 78)

    # Per-metric tag census — what XBRL tags do we have for this symbol?
    print(f"\n[{sym}] Tag census (distinct xbrl_tag per canonical metric):")
    for m in METRICS_DURATION:
        rows = conn.execute(
            """
            SELECT xbrl_tag, period_type, COUNT(*) as n, MIN(end_date), MAX(end_date)
              FROM facts
             WHERE symbol = ? AND metric = ?
          GROUP BY xbrl_tag, period_type
          ORDER BY n DESC
            """,
            (sym, m),
        ).fetchall()
        if not rows:
            print(f"  {m:28s}  (no rows)")
            continue
        for r in rows:
            tag, ptype, n, d_min, d_max = r
            print(f"  {m:28s}  tag={tag:55s}  type={ptype:8s}  n={n:3d}  "
                  f"range={d_min}..{d_max}")

    # Latest 6 duration facts per metric — show the actual values
    print(f"\n[{sym}] Latest 6 10-K duration facts per metric:")
    for m in METRICS_DURATION:
        rows = conn.execute(
            """
            SELECT start_date, end_date, fiscal_year, fiscal_period, form,
                   value, xbrl_tag
              FROM facts
             WHERE symbol = ? AND metric = ?
               AND period_type = 'duration'
               AND form = '10-K'
          ORDER BY end_date DESC
             LIMIT 6
            """,
            (sym, m),
        ).fetchall()
        if not rows:
            print(f"\n  -- {m} (duration, 10-K) --  (no rows)")
            continue
        print(f"\n  -- {m} (duration, 10-K) --")
        print(f"  {'start':10s}  {'end':10s}  {'fy':>6s}  {'per':>4s}  "
              f"{'form':>4s}  {'value':>12s}  tag")
        for r in rows:
            s, e, fy, fp, form, v, tag = r
            print(f"  {s!s:10s}  {e!s:10s}  {fy!s:>6s}  {fp!s:>4s}  "
                  f"{form!s:>4s}  {_fmt(v):>12s}  {tag}")

    # Derived sanity for most recent fiscal year
    print(f"\n[{sym}] Derived check (most recent 10-K, annual duration):")

    def _latest_annual(metric: str):
        row = conn.execute(
            """
            SELECT value, end_date, xbrl_tag
              FROM facts
             WHERE symbol = ? AND metric = ?
               AND period_type = 'duration'
               AND form = '10-K'
          ORDER BY end_date DESC
             LIMIT 1
            """,
            (sym, metric),
        ).fetchone()
        return row

    rev = _latest_annual('Revenue')
    cor = _latest_annual('CostOfRevenue')
    gp = _latest_annual('GrossProfit')
    opinc = _latest_annual('OperatingIncomeLoss')
    ocf = _latest_annual('OperatingCashFlow')
    cpx = _latest_annual('CapEx')

    def _v(r):
        return r[0] if r else None

    rev_v, cor_v, gp_v, oi_v = _v(rev), _v(cor), _v(gp), _v(opinc)
    ocf_v, cpx_v = _v(ocf), _v(cpx)
    end_r = rev[1] if rev else '?'

    print(f"  FY end:               {end_r}")
    print(f"  Revenue:              {_fmt(rev_v)}  "
          f"(tag={rev[2] if rev else '-'})")
    print(f"  CostOfRevenue:        {_fmt(cor_v)}  "
          f"(tag={cor[2] if cor else '-'})")
    print(f"  GrossProfit:          {_fmt(gp_v)}  "
          f"(tag={gp[2] if gp else '-'})")
    if rev_v and cor_v:
        implied_gp = rev_v - cor_v
        implied_gm = implied_gp / rev_v
        print(f"  Implied GP (rev-cor): {_fmt(implied_gp)}  "
              f"=> gross_margin = {implied_gm:.1%}")
    if rev_v and gp_v:
        print(f"  GP-based gross_margin:  {(gp_v/rev_v):.1%}")
    print(f"  OperatingIncomeLoss:  {_fmt(oi_v)}  "
          f"(tag={opinc[2] if opinc else '-'})")
    print(f"  OperatingCashFlow:    {_fmt(ocf_v)}  "
          f"(tag={ocf[2] if ocf else '-'})")
    print(f"  CapEx:                {_fmt(cpx_v)}  "
          f"(tag={cpx[2] if cpx else '-'})")
    if ocf_v is not None:
        fcf = ocf_v - (cpx_v or 0)
        print(f"  Implied FCF:          {_fmt(fcf)}  "
              f"=> fcf_margin = {(fcf/rev_v):.1%}"
              if rev_v else "")


def main():
    if not DB_PATH.exists():
        print(f"fundamentals.db not found at {DB_PATH}")
        raise SystemExit(1)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        for sym in SYMBOLS:
            dump_symbol(conn, sym)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
