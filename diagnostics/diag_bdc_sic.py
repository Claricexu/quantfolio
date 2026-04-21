"""Phase 1.9b — probe SEC submissions JSON for BDCs to see what SIC
they actually return.

Context: Phase 1.1 catches GS (SIC 6211) as CAT_C_FINANCIAL cleanly,
but 15 BDCs in the UNKNOWN bucket have BLANK sic in universe_raw.csv.
Summary claim was "SEC doesn't return SIC for '40 Act filers" but
that was a hypothesis — this script verifies by hitting
data.sec.gov/submissions/CIK{...}.json directly and printing:

  sic, sicDescription, category, entityType, name, ticker(s), exchanges

for 3 known BDCs (ARCC, OBDC, MAIN) + 1 known-good anchor (MSFT).

If SEC returns real SIC for the BDCs (6726 or similar):
  → our _fetch_sec_submission_ext() has a parsing bug; fix it, extend
    excluded_sic_ranges, done cheap.

If SEC returns blank sic for BDCs:
  → we need an alternate SIC source (yfinance sector? EDGAR filer-type?)
    OR a blank-SIC-defaults-to-exclude rule. Bigger design call.

Usage:
    python diag_bdc_sic.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

# SEC requires an identifying User-Agent with a real contact email.
# Configure SEC_USER_AGENT in your .env (see .env.example).
UA = os.environ.get(
    "SEC_USER_AGENT",
    "Quantfolio-Phase1.9b-Diagnostic quantfolio-user@example.com",
)
SLEEP = 0.6

# 3 BDCs with known blank SIC in our universe_raw.csv + 1 anchor
# for comparison.
PROBES = [
    ('ARCC', '0001287750'),  # Ares Capital Corp — largest BDC
    ('OBDC', '0001655888'),  # Blue Owl Capital Corp
    ('MAIN', '0001396440'),  # Main Street Capital
    ('MSFT', '0000789019'),  # Microsoft — known-good anchor
]


def _fetch_submissions(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main():
    print("Hitting data.sec.gov/submissions/CIK{10d}.json for each ticker.")
    print("Printing the key identity fields SEC returns in the top-level JSON.")
    print()

    for sym, cik in PROBES:
        print('=' * 80)
        print(f"  {sym}  (CIK {cik})")
        print('=' * 80)
        try:
            data = _fetch_submissions(cik)
        except Exception as e:
            print(f"  fetch error: {type(e).__name__}: {e}")
            print()
            time.sleep(SLEEP)
            continue

        # Key identity fields. Print whatever is there; blanks render
        # as '' which is the whole point.
        for key in ('name', 'sic', 'sicDescription', 'category',
                    'entityType', 'tickers', 'exchanges',
                    'ein', 'stateOfIncorporation',
                    'investorWebsite', 'website',
                    'fiscalYearEnd',
                    'mailingAddress'):
            v = data.get(key)
            if isinstance(v, dict):
                # Truncate nested dicts to their keys for readability
                v = f"{{keys: {list(v.keys())}}}"
            elif isinstance(v, list) and len(v) > 5:
                v = f"[{len(v)} items: {v[:3]}...]"
            print(f"  {key:22s} = {v!r}")

        # Count how many filings are in 'recent' (vs paginated)
        recent = ((data.get('filings') or {}).get('recent') or {}).get('form') or []
        files = ((data.get('filings') or {}).get('files') or [])
        print(f"  filings.recent.form    = [{len(recent)} entries]")
        print(f"  filings.files          = [{len(files)} pages of older filings]")

        print()
        time.sleep(SLEEP)


if __name__ == '__main__':
    main()
