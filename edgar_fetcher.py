"""
edgar_fetcher.py
─────────────────
Fetches SEC XBRL fundamental data (10-K + 10-Q filings) into a local SQLite cache.

Uses SEC's public companyfacts API directly — no third-party SEC library needed.
   https://data.sec.gov/api/xbrl/companyfacts/CIK<NNNNNNNNNN>.json

Caches the last N fiscal years (10-K) + last N quarters (10-Q) per ticker.
Per-ticker TTL: 90 days. Force-refresh with --refresh flag.

Tickers that aren't SEC-reporting issuers (ETFs like SPY/QQQ, foreign ADRs like
NTDOY/NTTYY) are recorded with status='not_in_sec' and gracefully skipped.

Usage:
    python edgar_fetcher.py --ticker AAPL
    python edgar_fetcher.py --all
    python edgar_fetcher.py --ticker AAPL --refresh
    python edgar_fetcher.py --list
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = HERE / "fundamentals.db"
TICKERS_CSV = HERE / "Tickers.csv"

# SEC requires identifying User-Agent per https://www.sec.gov/os/accessing-edgar-data
USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "Quantfolio Research Bot contact@quantfolio.local"
)
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# SIC / sicDescription live on submissions, NOT companyfacts
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# Paginated older submissions. SEC's `filings.recent` only holds the last
# ~1000 filings — for heavy filers (mega-caps with many 8-Ks) that can be
# just 2-3 years of history, pushing older 10-Ks/10-Qs out of `recent`. The
# `filings.files[].name` field points to additional JSONs at this URL.
SEC_SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{name}"

TTL_DAYS = 90
RATE_LIMIT_SLEEP = 0.12   # ~8 req/sec, under SEC's 10 req/sec cap
REQUEST_TIMEOUT = 30

# Canonical metric name → list of XBRL tags tried in order (first hit wins)
XBRL_TAG_CHAINS = {
    'Revenue': [
        # Phase 1.9b (2026-04-19): added the `Including...` variant after
        # CRWD surfaced as UNKNOWN/INSUFFICIENT_DATA. Both are legitimate
        # ASC 606 top-line tags — "Excluding" = net of pass-through sales
        # taxes, "Including" = gross. For B2B SaaS (buyer files tax) the
        # two numbers are identical; filers pick one by convention and
        # CRWD/peers use "Including". Put "Excluding" first so filers who
        # report both (legal and observed) keep the net value via dedup.
        'RevenueFromContractWithCustomerExcludingAssessedTax',
        'RevenueFromContractWithCustomerIncludingAssessedTax',
        'Revenues',
        'SalesRevenueNet',
        'SalesRevenueGoodsNet',
        # Phase 1.9b (2026-04-19): utility-sector revenue — MGEE (and
        # likely other SIC 4911-4939 regulated utilities) file only this
        # tag, not Revenues/Including. Appended last so filers that
        # report both keep the canonical value via first-wins dedup.
        'RegulatedAndUnregulatedOperatingRevenue',
    ],
    'GrossProfit': ['GrossProfit'],
    'CostOfRevenue': [
        'CostOfRevenue',
        'CostOfGoodsAndServicesSold',
        'CostOfGoodsSold',
    ],
    'OperatingIncomeLoss': ['OperatingIncomeLoss'],
    'NetIncomeLoss': ['NetIncomeLoss'],
    'InterestExpense': ['InterestExpense', 'InterestExpenseDebt'],
    'OperatingCashFlow': [
        'NetCashProvidedByUsedInOperatingActivities',
        'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations',
    ],
    'CapEx': [
        'PaymentsToAcquirePropertyPlantAndEquipment',
        'PaymentsForCapitalImprovements',
    ],
    'SharesOutstanding': [
        'CommonStockSharesOutstanding',
        'EntityCommonStockSharesOutstanding',
    ],
    # Phase 1.7c: weighted-average shares are natively split-adjusted by the
    # filer (they show up divided by the split ratio automatically once a
    # split is declared). Use these — NOT the instant SharesOutstanding —
    # for dilution detection. The GAAP `Basic` denominator is the primary
    # choice; fall back to diluted if basic isn't filed.
    'WeightedAverageSharesBasic': [
        'WeightedAverageNumberOfSharesOutstandingBasic',
        'WeightedAverageNumberOfDilutedSharesOutstanding',
    ],
    'Assets': ['Assets'],
    'Liabilities': ['Liabilities'],
    'LiabilitiesCurrent': ['LiabilitiesCurrent'],
    'Cash': [
        'CashAndCashEquivalentsAtCarryingValue',
        'Cash',
        'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
    ],
    'LongTermDebt': ['LongTermDebtNoncurrent', 'LongTermDebt'],
    'StockholdersEquity': ['StockholdersEquity'],
    'RnD': ['ResearchAndDevelopmentExpense'],
    'SGA': ['SellingGeneralAndAdministrativeExpense'],
}

# Metrics that live in the dei (entity-level) namespace as a fallback
DEI_FALLBACK = {'SharesOutstanding'}

# ─── DB ───────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    symbol TEXT PRIMARY KEY,
    cik TEXT,
    name TEXT,
    sic TEXT,
    sic_description TEXT,
    last_fetched TEXT,
    status TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    accession TEXT NOT NULL,
    form TEXT,
    fiscal_period TEXT,
    fiscal_year INTEGER,
    end_date TEXT,
    filed_date TEXT,
    UNIQUE(symbol, accession)
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    accession TEXT,
    metric TEXT NOT NULL,
    xbrl_tag TEXT,
    value REAL,
    unit TEXT,
    period_type TEXT,
    start_date TEXT,
    end_date TEXT,
    fiscal_period TEXT,
    fiscal_year INTEGER,
    form TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_symbol_metric ON facts(symbol, metric);
CREATE INDEX IF NOT EXISTS idx_facts_symbol_date ON facts(symbol, end_date);
CREATE INDEX IF NOT EXISTS idx_facts_symbol_form_metric ON facts(symbol, form, metric, end_date);
"""


def get_db():
    """Open the fundamentals SQLite DB, creating schema if missing."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


# ─── HTTP ─────────────────────────────────────────────────────────────────────

from http_client import get_json as _http_get_json, TokenBucket

# Module-level token bucket. SEC's public cap is 10 req/s; we run at 8/s.
# capacity=2 gives a small cushion so a retry landing next to a real call
# doesn't stall, without amplifying future concurrency — a 2-token burst
# from a cold bucket lands at ~10/s worst-case, still under the ceiling.
SEC_BUCKET = TokenBucket(rate_per_sec=8.0, capacity=2)


def http_get_json(url):
    """Fetch JSON from ``url`` via the shared http_client (Phase 4.1 of C-4).

    Delegates to :func:`http_client.get_json` with :data:`SEC_BUCKET` so every
    SEC call is paced through the same token bucket and inherits the retry +
    Retry-After contract. 404s propagate unchanged as :class:`urllib.error.HTTPError`
    so :func:`fetch_one`'s ticker-not-in-taxonomy branch keeps working.
    """
    return _http_get_json(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
        rate_limiter=SEC_BUCKET,
    )


# ─── Ticker → CIK map (cached in memory) ─────────────────────────────────────

_ticker_cik_map = None

def load_ticker_cik_map():
    global _ticker_cik_map
    if _ticker_cik_map is not None:
        return _ticker_cik_map
    data = http_get_json(SEC_TICKERS_URL)
    m = {}
    for entry in data.values():
        m[entry['ticker'].upper()] = {
            'cik': int(entry['cik_str']),
            'name': entry.get('title', ''),
        }
    _ticker_cik_map = m
    return m


def _fetch_submission_meta(cik):
    """Pull (sic, sic_description) from the SEC submissions endpoint.

    Non-fatal on failure — returns (None, None) so the main fetch still
    succeeds; we just won't have sector info for that ticker."""
    try:
        doc = http_get_json(SEC_SUBMISSIONS_URL.format(cik=cik))
    except Exception as e:
        print(f"  [warn] submissions fetch failed for CIK {cik}: {e}")
        return (None, None)
    sic = str(doc.get('sic', '') or '').strip() or None
    sic_desc = (doc.get('sicDescription', '') or '').strip() or None
    return (sic, sic_desc)


# ─── Core fetcher ─────────────────────────────────────────────────────────────

def is_fresh(conn, symbol):
    row = conn.execute(
        "SELECT last_fetched, status FROM tickers WHERE symbol=?",
        (symbol,)
    ).fetchone()
    if not row or not row['last_fetched'] or row['status'] != 'ok':
        return False
    try:
        dt = datetime.fromisoformat(row['last_fetched'])
    except Exception:
        return False
    return (datetime.now() - dt) < timedelta(days=TTL_DAYS)


def _mark_ticker(conn, symbol, cik, name, status, error=None, sic=None, sic_desc=None):
    conn.execute(
        "INSERT OR REPLACE INTO tickers(symbol, cik, name, sic, sic_description, "
        "last_fetched, status, error) VALUES (?,?,?,?,?,?,?,?)",
        (symbol, str(cik) if cik else None, name, sic, sic_desc,
         datetime.now().isoformat(), status, error)
    )


def fetch_one(symbol, conn, force=False):
    """Fetch XBRL facts for a single ticker. Returns status string."""
    symbol = symbol.upper().strip()
    if not force and is_fresh(conn, symbol):
        print(f"  [skip] {symbol} (cache fresh)")
        return 'skipped'

    # 1. Ticker → CIK
    try:
        cik_map = load_ticker_cik_map()
    except Exception as e:
        print(f"  [error] {symbol}: can't load CIK map: {e}")
        return 'error'

    if symbol not in cik_map:
        _mark_ticker(conn, symbol, None, None, 'not_in_sec',
                     'not an SEC-reporting issuer (ETF/ADR/foreign?)')
        conn.commit()
        print(f"  [skip] {symbol}: not in SEC ticker list (ETF/ADR?)")
        return 'not_found'

    cik = cik_map[symbol]['cik']
    name = cik_map[symbol]['name']

    # 2. CIK → facts JSON
    url = SEC_FACTS_URL.format(cik=cik)
    try:
        facts_doc = http_get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _mark_ticker(conn, symbol, cik, name, 'no_facts',
                         f"HTTP 404: no facts for CIK {cik}")
            conn.commit()
            print(f"  [skip] {symbol}: no XBRL facts at SEC (CIK {cik})")
            return 'no_facts'
        print(f"  [error] {symbol}: HTTP {e.code}: {e}")
        return 'error'
    except Exception as e:
        print(f"  [error] {symbol}: fetch failed: {e}")
        return 'error'

    us_gaap = facts_doc.get('facts', {}).get('us-gaap', {})
    dei = facts_doc.get('facts', {}).get('dei', {})

    # Clear prior data for this ticker
    conn.execute("DELETE FROM facts WHERE symbol=?", (symbol,))
    conn.execute("DELETE FROM filings WHERE symbol=?", (symbol,))

    filing_keys = set()
    n_facts = 0

    for canonical, tag_chain in XBRL_TAG_CHAINS.items():
        # Merge facts across ALL tags in the chain (was: stop at first hit).
        #
        # Why: companies migrate XBRL concept names over time due to taxonomy
        # updates. E.g. NVDA used `RevenueFromContractWithCustomerExcludingAssessedTax`
        # for FY20-FY22 but switched afterward — with the old break-at-first-hit
        # logic, we captured the old data and SILENTLY missed FY23/FY24/FY25
        # entirely, making `latest_ttm` return a 4-year-old annual value.
        #
        # Dedup by (accn, start, end) so when a single filing tags the same
        # fact under two aliased concepts we don't double-count. Priority:
        # whichever tag appears first in the chain wins for an identical key.
        seen_keys = set()
        for tag in tag_chain:
            src = us_gaap.get(tag)
            if src is None and canonical in DEI_FALLBACK:
                src = dei.get(tag)
            if not src:
                continue

            units = src.get('units', {})
            # Both instant share counts (SharesOutstanding) and the weighted-
            # average annual denominators (Phase 1.7c) live under the 'shares'
            # unit, not 'USD'. Expand this set when adding new share-unit metrics.
            if canonical in ('SharesOutstanding', 'WeightedAverageSharesBasic'):
                rows = units.get('shares', [])
                unit_label = 'shares'
            else:
                rows = units.get('USD', [])
                unit_label = 'USD'
            if not rows:
                continue

            for r in rows:
                form = r.get('form')
                if form not in ('10-K', '10-Q'):
                    continue
                fy = r.get('fy')
                fp = r.get('fp')
                accn = r.get('accn')
                start = r.get('start')
                end = r.get('end')
                val = r.get('val')
                filed = r.get('filed')
                if end is None or val is None:
                    continue
                dedup_key = (accn, start, end)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                period_type = 'instant' if start is None else 'duration'
                conn.execute(
                    "INSERT INTO facts(symbol, accession, metric, xbrl_tag, value, unit, "
                    "period_type, start_date, end_date, fiscal_period, fiscal_year, form) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (symbol, accn, canonical, tag, val, unit_label,
                     period_type, start, end, fp, fy, form)
                )
                n_facts += 1
                if accn and accn not in filing_keys:
                    filing_keys.add(accn)
                    conn.execute(
                        "INSERT OR IGNORE INTO filings(symbol, accession, form, "
                        "fiscal_period, fiscal_year, end_date, filed_date) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (symbol, accn, form, fp, fy, end, filed)
                    )

    # SIC / sicDescription come from the submissions endpoint (not companyfacts).
    # Phase 4.1 (C-4): pacing is now handled inside http_get_json via SEC_BUCKET,
    # so the explicit time.sleep(RATE_LIMIT_SLEEP) that used to live here is gone.
    sic_code, sic_desc = _fetch_submission_meta(cik)

    _mark_ticker(conn, symbol, cik, name, 'ok', None,
                 sic=sic_code, sic_desc=sic_desc)
    conn.commit()
    print(f"  [ok] {symbol}: {n_facts} facts across {len(filing_keys)} filings"
          f"  sector={sic_desc or '?'}")
    return 'ok'


def fetch_all(tickers, conn, force=False):
    results = {'ok': 0, 'skipped': 0, 'not_found': 0, 'no_facts': 0, 'error': 0}
    for i, sym in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {sym}")
        try:
            r = fetch_one(sym, conn, force=force)
            results[r] = results.get(r, 0) + 1
        except Exception as e:
            print(f"  [error] {sym}: {e}")
            results['error'] += 1
        # Phase 4.1 (C-4): SEC_BUCKET inside http_get_json handles pacing now;
        # no explicit sleep needed between tickers.
    return results


# ─── Utilities ────────────────────────────────────────────────────────────────

def load_tickers_from_csv():
    if not TICKERS_CSV.exists():
        return []
    out = []
    for line in TICKERS_CSV.read_text().splitlines():
        s = line.strip().upper()
        if s:
            out.append(s)
    return out


def _load_universe_symbols(csv_path):
    """Read a CSV with a 'symbol' column and return uppercase symbols.

    Used by the --universe flag to batch-fetch Layer 1's prescreened universe.
    Accepts either a proper CSV (DictReader-parseable, with 'symbol' header)
    or a plain one-symbol-per-line file as a fallback.
    """
    import csv as _csv
    path = Path(csv_path)
    if not path.exists():
        print(f"  [error] universe CSV not found: {path}")
        return []
    with path.open('r', newline='', encoding='utf-8') as f:
        sample = f.read(2048)
        f.seek(0)
        header_line = next(iter(sample.lower().splitlines()), '')
        if 'symbol' in header_line:
            reader = _csv.DictReader(f)
            return [r['symbol'].strip().upper()
                    for r in reader
                    if r.get('symbol') and r['symbol'].strip()]
        # Plain list fallback (one symbol per line)
        return [line.strip().upper() for line in f.read().splitlines()
                if line.strip()]


# ─── Read API (used by fundamental_metrics.py) ───────────────────────────────

def get_facts(symbol, metric, form=None, limit=None, conn=None):
    """Return rows from `facts` for symbol+metric, newest first."""
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    sql = "SELECT * FROM facts WHERE symbol=? AND metric=?"
    params = [symbol, metric]
    if form:
        sql += " AND form=?"
        params.append(form)
    sql += " ORDER BY end_date DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if close_conn:
        conn.close()
    return rows


def get_ticker_info(symbol, conn=None):
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    row = conn.execute(
        "SELECT * FROM tickers WHERE symbol=?", (symbol.upper(),)
    ).fetchone()
    if close_conn:
        conn.close()
    return dict(row) if row else None


def get_all_cached_symbols(status='ok', conn=None):
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    rows = conn.execute(
        "SELECT symbol FROM tickers WHERE status=? ORDER BY symbol", (status,)
    ).fetchall()
    if close_conn:
        conn.close()
    return [r['symbol'] for r in rows]


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Fetch SEC XBRL fundamental data into fundamentals.db"
    )
    p.add_argument("--ticker", help="Single ticker, e.g. AAPL")
    p.add_argument("--symbols", help="Comma-separated list, e.g. ISRG,SYK,ZBH")
    p.add_argument("--all", action="store_true", help="All tickers from Tickers.csv")
    p.add_argument("--universe", metavar="CSV",
                   help="Path to universe CSV with a 'symbol' column "
                        "(e.g. universe_prescreened.csv from Layer 1)")
    p.add_argument("--refresh", action="store_true", help="Force refresh (ignore 90-day TTL)")
    p.add_argument("--list", action="store_true", help="List cached tickers with status")
    args = p.parse_args()

    conn = get_db()

    if args.list:
        print(f"DB: {DB_PATH}")
        rows = conn.execute(
            "SELECT symbol, cik, status, last_fetched FROM tickers ORDER BY symbol"
        ).fetchall()
        if not rows:
            print("  (no tickers cached yet — run --ticker X or --all first)")
        for r in rows:
            print(f"  {r['symbol']:6s}  CIK={(r['cik'] or '-'):>10s}  "
                  f"status={r['status']:12s}  fetched={r['last_fetched']}")
        conn.close()
        return

    if args.ticker:
        fetch_one(args.ticker, conn, force=args.refresh)
    elif args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
        print(f"Fetching {len(syms)} tickers: {syms} (force={args.refresh})")
        t0 = time.time()
        res = fetch_all(syms, conn, force=args.refresh)
        print(f"\nDone in {round(time.time()-t0,1)}s: {res}")
    elif args.all:
        tickers = load_tickers_from_csv()
        if not tickers:
            print(f"No tickers in {TICKERS_CSV}")
            sys.exit(1)
        print(f"Fetching {len(tickers)} tickers (force={args.refresh}, UA={USER_AGENT!r})...")
        t0 = time.time()
        res = fetch_all(tickers, conn, force=args.refresh)
        elapsed = round(time.time() - t0, 1)
        print(f"\nDone in {elapsed}s: {res}")
    elif args.universe:
        tickers = _load_universe_symbols(args.universe)
        if not tickers:
            print(f"No symbols found in {args.universe}")
            sys.exit(1)
        print(f"Fetching {len(tickers)} tickers from universe CSV "
              f"(force={args.refresh}, UA={USER_AGENT!r})...")
        t0 = time.time()
        res = fetch_all(tickers, conn, force=args.refresh)
        elapsed = round(time.time() - t0, 1)
        print(f"\nDone in {elapsed}s: {res}")
    else:
        p.print_help()

    conn.close()


if __name__ == "__main__":
    main()
