"""Phase 1.9 followup — list every us-gaap tag CRWD actually files.

Hypothesis H1 confirmed by diag_crwd_revenue.py: none of our 4 Revenue-family
tag-chain entries hit CRWD's filings. But GrossProfit + CostOfRevenue ARE
present, so CRWD files an income statement — it just uses a tag name we
don't walk.

This script pulls CRWD's companyfacts JSON directly from SEC and prints:
  (A) All us-gaap tags that have USD duration facts, ranked by fact count
  (B) Same list filtered to names matching /revenue|sales|subscription|service/i
      — that's where the top-line hides
  (C) For the top ~5 matching tags, a peek at their latest 2 facts so we
      can sanity-check the magnitude matches CRWD's known ~$4.8B ARR

Usage:
    python diag_crwd_sec_tags.py

Read-only; no DB writes, one HTTP GET.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

# CRWD's CIK, zero-padded to 10 digits per SEC convention
CIK = "0001535527"
URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"

# SEC requires an identifying User-Agent with a real contact email.
# Configure SEC_USER_AGENT in your .env (see .env.example).
UA = os.environ.get(
    "SEC_USER_AGENT",
    "Quantfolio-Phase1.9-Diagnostic quantfolio-user@example.com",
)


def _fmt(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e9:
        return f"{v/1e9:,.3f}B"
    if abs(v) >= 1e6:
        return f"{v/1e6:,.1f}M"
    return f"{v:,.0f}"


def main():
    print(f"Fetching {URL} ...")
    req = urllib.request.Request(URL, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    us_gaap = data.get('facts', {}).get('us-gaap', {}) or {}
    print(f"CRWD files {len(us_gaap)} us-gaap tags total.")
    print()

    # Build a table: tag → (n_usd_duration_facts, latest_end, one_sample)
    # Only counting USD duration facts (Revenue-like things live here).
    table = []
    for tag, block in us_gaap.items():
        usd_rows = (block.get('units') or {}).get('USD') or []
        # duration = has 'start' key set (instant facts don't)
        dur = [r for r in usd_rows
               if r.get('start') and r.get('form') in ('10-K', '10-Q')]
        if not dur:
            continue
        latest = max(dur, key=lambda r: r.get('end') or '')
        table.append((tag, len(dur), latest.get('end'), latest.get('val'),
                      latest.get('form'), latest.get('start')))

    # (A) All tags, top 30 by fact count
    print('=' * 90)
    print("  (A) Top 30 us-gaap tags with USD duration facts (by fact count)")
    print('=' * 90)
    table.sort(key=lambda r: -r[1])
    for tag, n, e, v, form, s in table[:30]:
        print(f"  {tag[:58]:58s}  n={n:3d}  latest={e}  "
              f"{form}  {s}..{e}  val={_fmt(v)}")

    # (B) Filter to revenue-like names
    print()
    print('=' * 90)
    print("  (B) Tags matching /revenue|sales|subscription|service/i")
    print('=' * 90)
    pat = re.compile(r'revenue|sales|subscription|service', re.IGNORECASE)
    hits = [row for row in table if pat.search(row[0])]
    if not hits:
        print("  (no matches — CRWD uses filer-specific extension tags, "
              "not us-gaap. Revenue reconstruction via GrossProfit + "
              "CostOfRevenue may be the only path.)")
    else:
        for tag, n, e, v, form, s in hits:
            print(f"  {tag[:68]:68s}  n={n:3d}  "
                  f"latest={e}  val={_fmt(v)}")

    # (C) Peek at the top-3 revenue-matching tags — last 4 annual-ish facts
    print()
    print('=' * 90)
    print("  (C) Top-3 revenue-matching tags — last 4 annual (330-400 day) "
          "facts each")
    print('=' * 90)
    if not hits:
        print("  (none to peek at)")
    else:
        for tag, n, _, _, _, _ in hits[:3]:
            print(f"\n  --- {tag} ---")
            usd_rows = (us_gaap[tag].get('units') or {}).get('USD') or []
            annuals = []
            for r in usd_rows:
                s = r.get('start')
                e = r.get('end')
                if not (s and e):
                    continue
                try:
                    from datetime import datetime as _dt
                    days = (_dt.strptime(e[:10], "%Y-%m-%d")
                            - _dt.strptime(s[:10], "%Y-%m-%d")).days
                except Exception:
                    continue
                if 330 <= days <= 400 and r.get('form') == '10-K':
                    annuals.append((e, days, r.get('val'), r.get('fp'),
                                    r.get('fy'), r.get('accn')))
            annuals.sort(reverse=True)
            for e, d, v, fp, fy, acc in annuals[:4]:
                print(f"    end={e}  days={d}  fy={fy} fp={fp}  "
                      f"val={_fmt(v)}  accn={acc}")

    # (D) Cross-check: compute Revenue = GrossProfit + CostOfRevenue
    #     from what we already store in XBRL. If that reconstructs to ~$4.8B
    #     for FY2025 (ended Jan 2025), we know the workaround is viable.
    print()
    print('=' * 90)
    print("  (D) Sanity check: CRWD Revenue reconstructed as GrossProfit + "
          "CostOfRevenue")
    print('=' * 90)
    gp_usd = ((us_gaap.get('GrossProfit') or {}).get('units') or {}).get('USD') or []
    cor_tag = next((t for t in ('CostOfRevenue', 'CostOfGoodsAndServicesSold',
                                'CostOfGoodsSold') if t in us_gaap), None)
    cor_usd = []
    if cor_tag:
        cor_usd = ((us_gaap.get(cor_tag) or {}).get('units') or {}).get('USD') or []
        print(f"  CostOf* tag actually used: {cor_tag}  (n={len(cor_usd)})")
    else:
        print("  (no CostOfRevenue/CostOfGoodsAndServicesSold/CostOfGoodsSold)")

    def _pick_annuals(rows):
        out = []
        for r in rows:
            s, e = r.get('start'), r.get('end')
            if not (s and e) or r.get('form') != '10-K':
                continue
            try:
                from datetime import datetime as _dt
                days = (_dt.strptime(e[:10], "%Y-%m-%d")
                        - _dt.strptime(s[:10], "%Y-%m-%d")).days
            except Exception:
                continue
            if 330 <= days <= 400:
                out.append((e, r.get('val'), r.get('accn')))
        # keep max-accn per end_date (latest restatement)
        best = {}
        for e, v, a in out:
            if e not in best or (a or '') > (best[e][1] or ''):
                best[e] = (v, a)
        return sorted(best.items(), reverse=True)  # newest first

    gp_annuals = dict(_pick_annuals(gp_usd))
    cor_annuals = dict(_pick_annuals(cor_usd))
    print()
    print(f"  {'end':12s}  {'GP':>14s}  {'CoR':>14s}  {'Revenue=GP+CoR':>18s}")
    all_ends = sorted(set(gp_annuals) | set(cor_annuals), reverse=True)
    for e in all_ends[:6]:
        gp_v = gp_annuals.get(e, (None,))[0]
        cor_v = cor_annuals.get(e, (None,))[0]
        rev = gp_v + cor_v if (gp_v is not None and cor_v is not None) else None
        print(f"  {e:12s}  {_fmt(gp_v):>14s}  {_fmt(cor_v):>14s}  {_fmt(rev):>18s}")


if __name__ == '__main__':
    main()
