"""Phase 1.9b — batch SEC-tag probe across all CAT_A_TAG_MISS tickers.

Discovery from diag_unknown_triage.py (2026-04-19):
  UNKNOWN bucket split          CAT_A  CAT_B  CAT_C  CAT_D
                                   12     1      16     9
  CAT_A = rev_fact_count = 0, but OCF ingested → ingest ran cleanly, the
  Revenue tag chain just missed this filer's chosen tag.

CRWD (already fixed) used `RevenueFromContractWithCustomerIncludingAssessedTax`
— legitimate ASC 606 top-line, just not in our chain. This script extends
that investigation to the remaining 12 CAT_A tickers in one pass.

For each CAT_A ticker it fetches
  data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json
once and lists every us-gaap tag whose name matches
/revenue|sales|subscription|service/i AND has USD duration facts AND at
least one ~annual (330-400 day) window. Output:
  (A) per-ticker top 3 revenue-like tags (with in-chain / new status)
  (B) aggregated frequency: tags appearing across tickers — single-patch
      opportunities
  (C) suggested additions to XBRL_TAG_CHAINS['Revenue']

If section (C) is empty, CAT_A failures are NOT tag-chain misses and we
need a deeper hypothesis (period classification, form-type filter, etc.).

Usage:
    python diag_cat_a_tags.py

Read-only DB + ~12 HTTP GETs (rate-limited to SEC fair-use 0.6s floor).
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / 'fundamentals.db'
CSV_PATH = ROOT / 'screener_results.csv'

UA = "Quantfolio-Phase1.9b-Diagnostic xu.withoutwax@gmail.com"
SLEEP_BETWEEN_CALLS = 0.6  # SEC fair-use floor

# Mirror of edgar_fetcher.XBRL_TAG_CHAINS['Revenue'] post-1.9b patch.
# Keep this in sync if the chain changes — only used for [IN CHAIN]/[NEW]
# labelling and the section (C) diff.
CURRENT_CHAIN = {
    'RevenueFromContractWithCustomerExcludingAssessedTax',
    'RevenueFromContractWithCustomerIncludingAssessedTax',
    'Revenues',
    'SalesRevenueNet',
    'SalesRevenueGoodsNet',
}

# SIC codes diag_unknown_triage.py treats as CAT_C_FINANCIAL. Keep in sync
# with that script so CAT_A detection here matches.
FINANCIAL_SIC = {'6211', '6722', '6726', '6770', '6199'}

REV_PATTERN = re.compile(r'revenue|sales|subscription|service', re.IGNORECASE)


def _has(row, field):
    return (row.get(field) or '').strip() != ''


def _fmt(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e9:
        return f"{v/1e9:,.2f}B"
    if abs(v) >= 1e6:
        return f"{v/1e6:,.0f}M"
    return f"{v:,.0f}"


def _rev_fact_count(conn, symbol):
    r = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE symbol=? AND metric='Revenue'",
        (symbol,)
    ).fetchone()
    return r[0] if r else 0


def _get_cik(conn, symbol):
    r = conn.execute(
        "SELECT cik FROM tickers WHERE symbol=?",
        (symbol,)
    ).fetchone()
    return r[0] if r and r[0] is not None else None


def _identify_cat_a(conn, rows):
    """Apply the CAT_A_TAG_MISS signature from diag_unknown_triage.py
    exactly: UNKNOWN verdict, not CAT_C (financial/blank-sector), with
    rev_fact_count == 0 AND ocf ingested. Returns [(symbol, cik_10d)]."""
    out = []
    for r in rows:
        if (r.get('archetype') or '').upper() != 'UNKNOWN':
            continue
        sym = r.get('symbol') or ''
        if not sym:
            continue
        sic = (r.get('sic') or '').strip()
        sector = (r.get('sector') or '').strip()
        # CAT_C exclusion has priority (matches triage script ordering)
        if sic in FINANCIAL_SIC or sector == '':
            continue
        if not _has(r, 'operating_cash_flow_ttm'):
            continue
        if _rev_fact_count(conn, sym) != 0:
            continue
        cik = _get_cik(conn, sym)
        if cik is None:
            continue
        out.append((sym, str(cik).zfill(10)))
    return out


def _fetch_companyfacts(cik):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _revenue_tag_summary(facts):
    """For one companyfacts JSON, return a list of
      (tag, n_dur_usd, n_annual, latest_end, latest_val)
    for us-gaap tags where:
      - name matches REV_PATTERN
      - USD unit block exists
      - has 10-K/10-Q duration facts
      - at least one fact is a 330-400 day window (can source annual Revenue)
    Sorted by duration fact count descending."""
    us_gaap = (facts.get('facts') or {}).get('us-gaap') or {}
    out = []
    for tag, block in us_gaap.items():
        if not REV_PATTERN.search(tag):
            continue
        usd = (block.get('units') or {}).get('USD') or []
        dur = [r for r in usd
               if r.get('start') and r.get('form') in ('10-K', '10-Q')]
        if not dur:
            continue
        annuals = []
        for r in dur:
            try:
                s = datetime.strptime(r['start'][:10], "%Y-%m-%d").date()
                e = datetime.strptime(r['end'][:10], "%Y-%m-%d").date()
                if 330 <= (e - s).days <= 400:
                    annuals.append(r)
            except Exception:
                continue
        if not annuals:
            continue
        latest = max(annuals, key=lambda r: r.get('end') or '')
        out.append((tag, len(dur), len(annuals),
                    latest.get('end'), latest.get('val')))
    out.sort(key=lambda r: -r[1])
    return out


def main():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        raise SystemExit(1)
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found")
        raise SystemExit(1)

    conn = sqlite3.connect(str(DB_PATH))
    with CSV_PATH.open(encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    cat_a = _identify_cat_a(conn, rows)
    print(f"CAT_A_TAG_MISS tickers identified: {len(cat_a)}")
    if cat_a:
        print("  " + ", ".join(sym for sym, _ in cat_a))
    print()
    if not cat_a:
        print("  (nothing to probe — re-run diag_unknown_triage.py first)")
        conn.close()
        return

    per_ticker = {}
    for i, (sym, cik) in enumerate(cat_a, 1):
        print(f"[{i:2d}/{len(cat_a)}] {sym:6s} (CIK {cik}) ... ",
              end='', flush=True)
        try:
            facts = _fetch_companyfacts(cik)
            hits = _revenue_tag_summary(facts)
            per_ticker[sym] = hits
            print(f"{len(hits)} revenue-like tags with annual facts")
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}")
            per_ticker[sym] = None
        except Exception as e:
            print(f"ERROR {type(e).__name__}: {e}")
            per_ticker[sym] = None
        if i < len(cat_a):
            time.sleep(SLEEP_BETWEEN_CALLS)

    # (A) Per-ticker detail
    print()
    print('=' * 92)
    print("  (A) Per-ticker top 3 revenue-like tags "
          "(USD duration + at least 1 annual 330-400d fact)")
    print('=' * 92)
    for sym, hits in per_ticker.items():
        print()
        print(f"  --- {sym} ---")
        if hits is None:
            print("    (fetch failed)")
            continue
        if not hits:
            print("    (NO revenue-like tag has any annual USD facts — "
                  "likely a filer-specific extension outside the us-gaap "
                  "namespace, or the name misses /revenue|sales|subscription|service/i)")
            continue
        for tag, n_dur, n_ann, e, v in hits[:3]:
            status = "IN CHAIN" if tag in CURRENT_CHAIN else "NEW"
            print(f"    [{status:8s}] {tag[:58]:58s}  "
                  f"dur={n_dur:3d}  ann={n_ann:2d}  "
                  f"latest={e} {_fmt(v)}")

    # (B) Aggregated tag frequency
    print()
    print('=' * 92)
    print("  (B) Tag frequency across CAT_A tickers — one-patch-fixes-many targets")
    print('=' * 92)
    # Count the TOP tag per ticker (the one that matters most), plus the
    # #2 in case #1 is already in the chain (shouldn't happen for CAT_A
    # but be defensive).
    tag_counter = Counter()
    for hits in per_ticker.values():
        if not hits:
            continue
        for tag, *_ in hits[:2]:
            tag_counter[tag] += 1
    if not tag_counter:
        print("  (no tags collected)")
    else:
        print(f"  {'tag':60s} {'count':>5s}  status")
        for tag, c in tag_counter.most_common():
            status = "IN CHAIN" if tag in CURRENT_CHAIN else "NEW"
            print(f"  {tag:60s} {c:>5d}  {status}")

    # (C) Actionable patch
    print()
    print('=' * 92)
    print("  (C) Suggested additions to XBRL_TAG_CHAINS['Revenue']")
    print('=' * 92)
    new_tags = [t for t, _ in tag_counter.most_common()
                if t not in CURRENT_CHAIN]
    if not new_tags:
        print("  (none — every ticker's primary revenue tag is already in")
        print("   the chain. CAT_A failures are therefore NOT tag-chain")
        print("   misses; investigate period classification / form filter /")
        print("   filer-specific extensions instead.)")
    else:
        print("  Add these tags to XBRL_TAG_CHAINS['Revenue'] in")
        print("  edgar_fetcher.py (ordered by per-ticker frequency):")
        print()
        for t in new_tags:
            print(f"    '{t}',  # fixes {tag_counter[t]} ticker(s)")
        print()
        print(f"  After editing the chain, re-fetch the {len(cat_a)} "
              f"CAT_A tickers with --refresh to pull these tags.")

    conn.close()


if __name__ == '__main__':
    main()
