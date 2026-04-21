"""Phase 1.9b followup — HARDENED SEC-tag probe for the 4 stubborn CAT_A cases.

v1 (diag_cat_a_tags.py) caught 7 stale-DB false positives correctly, but
its /revenue|sales|subscription|service/i regex polluted section (B)/(C)
with false matches like:
  - AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue  (stock comp, matches 'Service')
  - CostOfRevenue  (the cost, matches 'Revenue')
  - EmployeeServiceShareBasedCompensationAllocation...  (stock comp)
  - ExciseAndSalesTaxes  (tax collection, matches 'Sales')

This v2:
  (1) drops `subscription|service` from the include pattern — too noisy
  (2) applies an explicit false-positive blocklist (Cost, Compensation,
      ShareBased, Adjustment, Increase/Decrease, ComprehensiveIncome,
      ProForma, Amortization, Gain, Loss, Allocation, Excise*, etc.)
  (3) shows top 10 per ticker (not top 3) so buried legit tags surface
  (4) adds an explicit canonical-tag presence check per ticker
      (Revenue / Revenues / RevenueFromContractWithCustomer{Excl,Incl}...)
      so we can see at a glance whether the filer uses any us-gaap
      standard tag at all

Default targets: VLO, APA, FSLY, CUK (the 4 CAT_A cases v1 couldn't
resolve — not stale-DB, genuinely using non-standard tags or extensions).
MGEE is separately handled by the `RegulatedAndUnregulatedOperatingRevenue`
patch already landing in edgar_fetcher.py.

Usage:
    python diag_cat_a_tags_v2.py
    python diag_cat_a_tags_v2.py VLO APA FSLY CUK      # explicit list
    python diag_cat_a_tags_v2.py VLO                    # one ticker

Read-only; one HTTP GET per ticker (0.6s rate-limit floor).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / 'fundamentals.db'

# SEC requires an identifying User-Agent with a real contact email.
# Configure SEC_USER_AGENT in your .env (see .env.example).
UA = os.environ.get(
    "SEC_USER_AGENT",
    "Quantfolio-Phase1.9b-Diagnostic quantfolio-user@example.com",
)
SLEEP_BETWEEN_CALLS = 0.6

DEFAULT_TICKERS = ['VLO', 'APA', 'FSLY', 'CUK']

CANONICAL_REV_TAGS = [
    'Revenue',
    'Revenues',
    'RevenueFromContractWithCustomerExcludingAssessedTax',
    'RevenueFromContractWithCustomerIncludingAssessedTax',
    'SalesRevenueNet',
    'SalesRevenueGoodsNet',
    'SalesRevenueServicesNet',
]

# Hardened revenue-tag filter: must look revenue-family AND not be a known
# false-positive substring.
REV_INCLUDE = re.compile(r'revenue|sales', re.IGNORECASE)

REV_EXCLUDE_SUBSTRINGS = [
    # Costs / expenses that contain "Revenue" or "Sales" in their names
    'Cost',
    # Stock-based compensation family (all trigger the v1 'Service' regex trap)
    'Compensation',
    'ShareBased',
    'Allocation',
    # Adjustments / reclasses — not a revenue flow
    'Adjustment',
    # Balance-sheet accruals / changes in balance-sheet items
    'Increase',
    'Decrease',
    'Accrued',
    'Deferred',  # DeferredRevenue is a liability, not a flow
    # Other comprehensive income / OCI — not operating revenue
    'ComprehensiveIncome',
    # Pro forma / hypothetical — not actual revenue
    'ProForma',
    # Tax collections (ExciseAndSalesTaxes); legit revenue-tax tags
    # (like *AssessedTax) don't start with Excise
    'Excise',
    # Amortization / gain / loss — income-statement items mislabelled
    'Amortization',
    'Amortiz',
    'Gain',
    'Loss',
    # Other inventory / reserve items
    'Inventory',
    'Reserve',
]


def _is_revenue_like(tag: str) -> bool:
    if not REV_INCLUDE.search(tag):
        return False
    for bad in REV_EXCLUDE_SUBSTRINGS:
        if bad in tag:
            return False
    return True


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


def _get_cik(conn, symbol):
    r = conn.execute(
        "SELECT cik FROM tickers WHERE symbol=?",
        (symbol,)
    ).fetchone()
    return str(r[0]).zfill(10) if r and r[0] is not None else None


def _fetch_companyfacts(cik):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _annual_span_days(r):
    try:
        s = datetime.strptime(r['start'][:10], "%Y-%m-%d").date()
        e = datetime.strptime(r['end'][:10], "%Y-%m-%d").date()
        return (e - s).days
    except Exception:
        return None


def _tag_summary(us_gaap, tag):
    """Return (n_dur, n_annual, latest_end, latest_val) for tag, or None
    if the tag has no USD duration facts at all."""
    block = us_gaap.get(tag) or {}
    usd = (block.get('units') or {}).get('USD') or []
    dur = [r for r in usd
           if r.get('start') and r.get('form') in ('10-K', '10-Q')]
    if not dur:
        return None
    annuals = [r for r in dur
               if (d := _annual_span_days(r)) is not None and 330 <= d <= 400]
    if annuals:
        latest = max(annuals, key=lambda r: r.get('end') or '')
    else:
        latest = max(dur, key=lambda r: r.get('end') or '')
    return (len(dur), len(annuals), latest.get('end'), latest.get('val'))


def _scan(us_gaap):
    """Return list of (tag, n_dur, n_annual, latest_end, latest_val) for
    every us-gaap tag passing the hardened revenue-like filter and having
    USD duration facts. Sorted by n_dur descending."""
    out = []
    for tag, block in us_gaap.items():
        if not _is_revenue_like(tag):
            continue
        s = _tag_summary(us_gaap, tag)
        if s is None:
            continue
        out.append((tag, *s))
    out.sort(key=lambda r: -r[1])
    return out


def _scan_extensions(all_facts, rev_tag_ranks):
    """Peek at filer-specific namespaces (dei, srt, and the filer's own
    extension taxonomy) for revenue-like tags. Helps explain CAT_A when
    us-gaap namespace is empty of real revenue but the filer has their
    own `fsly:SubscriptionRevenue` style extension.

    Returns {namespace: [(tag, n_dur, n_annual, latest_end, latest_val), ...]}
    (only non-empty namespaces included)."""
    out = {}
    for ns, concepts in all_facts.items():
        if ns == 'us-gaap':
            continue
        hits = []
        for tag, block in concepts.items():
            if not _is_revenue_like(tag):
                continue
            # same summary logic
            usd = (block.get('units') or {}).get('USD') or []
            dur = [r for r in usd
                   if r.get('start') and r.get('form') in ('10-K', '10-Q')]
            if not dur:
                continue
            annuals = [r for r in dur
                       if (d := _annual_span_days(r)) is not None and 330 <= d <= 400]
            latest = max(annuals if annuals else dur,
                         key=lambda r: r.get('end') or '')
            hits.append((tag, len(dur), len(annuals),
                         latest.get('end'), latest.get('val')))
        if hits:
            hits.sort(key=lambda r: -r[1])
            out[ns] = hits
    return out


def probe_one(conn, symbol):
    print('=' * 92)
    print(f"  {symbol}")
    print('=' * 92)

    cik = _get_cik(conn, symbol)
    if cik is None:
        print(f"  (no CIK in tickers table — skipping)")
        return
    print(f"  CIK: {cik}")

    try:
        facts = _fetch_companyfacts(cik)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — skipping")
        return
    except Exception as e:
        print(f"  fetch error {type(e).__name__}: {e}")
        return

    all_facts = facts.get('facts') or {}
    us_gaap = all_facts.get('us-gaap') or {}
    print(f"  us-gaap tags total: {len(us_gaap)}")
    namespaces_present = [ns for ns in all_facts if ns != 'us-gaap']
    if namespaces_present:
        print(f"  other namespaces present: {', '.join(namespaces_present)}")

    # (1) Canonical-tag presence check
    print()
    print(f"  (1) CANONICAL us-gaap revenue tag presence:")
    print(f"      {'tag':60s} {'dur':>4s} {'ann':>3s}  latest")
    for tag in CANONICAL_REV_TAGS:
        s = _tag_summary(us_gaap, tag)
        if s is None:
            print(f"      {tag:60s} {'—':>4s} {'—':>3s}  (not filed)")
        else:
            n_dur, n_ann, e, v = s
            print(f"      {tag:60s} {n_dur:>4d} {n_ann:>3d}  "
                  f"{e} {_fmt(v)}")

    # (2) Top-10 filtered us-gaap revenue-like tags
    print()
    print(f"  (2) us-gaap revenue-family tags after false-positive filter:")
    hits = _scan(us_gaap)
    if not hits:
        print(f"      (none — filer likely uses extension taxonomy only)")
    else:
        print(f"      {'tag':60s} {'dur':>4s} {'ann':>3s}  latest")
        for tag, n_dur, n_ann, e, v in hits[:10]:
            print(f"      {tag:60s} {n_dur:>4d} {n_ann:>3d}  "
                  f"{e} {_fmt(v)}")

    # (3) Extension-namespace revenue-like tags
    ext = _scan_extensions(all_facts, hits)
    if ext:
        print()
        print(f"  (3) FILER-EXTENSION namespaces with revenue-like tags:")
        for ns, tag_list in ext.items():
            print(f"      --- {ns} ---")
            for tag, n_dur, n_ann, e, v in tag_list[:5]:
                print(f"      {tag:58s} {n_dur:>4d} {n_ann:>3d}  "
                      f"{e} {_fmt(v)}")


def main():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        raise SystemExit(1)

    tickers = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_TICKERS
    tickers = [t.upper() for t in tickers]

    print(f"Probing {len(tickers)} ticker(s) with hardened revenue-tag filter:")
    print(f"  {', '.join(tickers)}")
    print()

    conn = sqlite3.connect(str(DB_PATH))
    for i, sym in enumerate(tickers):
        if i > 0:
            time.sleep(SLEEP_BETWEEN_CALLS)
            print()
        probe_one(conn, sym)
    conn.close()


if __name__ == '__main__':
    main()
