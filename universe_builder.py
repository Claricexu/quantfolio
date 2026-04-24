"""
universe_builder.py
───────────────────
Layer 1 of the Quantfolio two-layer architecture.

Architecture: Option B — Unified Metadata Pass.
    All HTTP I/O lives in Phase 1.0. Phase 1.1 is pure local filter logic
    over `universe_raw.csv` — re-tunable in <1 min with zero network cost.

Phase 1.0 — Unified Metadata Gather:
    Three sub-stages, cheapest API first, each stage queries only survivors
    of the prior:
      Stage 1a (fast_info):       SEC ~10k tickers -> yfinance fast_info
                                  -> capture mcap, currency, exchange,
                                     3mo_avg_volume, last_price
                                  -> keep USD + US exchange + mcap >= $1B
      Stage 1b (.info):           yfinance .info on stage-1a survivors
                                  -> capture totalRevenue
                                  -> keep annual_revenue >= $10M
      Stage 1c (SEC submissions): data.sec.gov/submissions on stage-1b survivors
                                  -> capture sic, sic_description, n_10k, n_10q
                                  -> no filter (Phase 1.1 handles rejection)
    Writes `universe_raw.csv` (~1400 rows expected) with full per-ticker
    metadata — every field Phase 1.1 could need.

Phase 1.1 — Prescreen (pure-local, no HTTP):
    Reads `universe_raw.csv`, applies rules from `prescreen_rules.json`:
      A. Liquidity: avg_dollar_volume_90d >= $5M
      B. Data availability: n_10k >= 5 AND n_10q >= 10
      C. Framework applicability: sic NOT IN excluded_sic_ranges
         (excludes Banks 6020-6030, REITs 6798, investment-fund shells
          6199/6722/6770; keeps Insurance 6300-6411 + Utilities 4911-4939)
    Sorts passing rows by market cap, cuts to `target_size` (500).
    Writes `universe_prescreened.csv` with `prescreen_pass_reason` column.

Checkpointing:
    Each Phase 1.0 sub-stage has its own checkpoint CSV so any stage can
    resume independently:
      .universe_checkpoint.csv          (Stage 1a — fast_info)
      .universe_revenue_checkpoint.csv  (Stage 1b — .info revenue)
      .universe_sec_checkpoint.csv      (Stage 1c — SEC submissions)
    Pass `--no-resume` to rebuild from scratch.

Usage:
    python universe_builder.py --build            # runs 1.0 + 1.1 end-to-end
    python universe_builder.py --raw-only         # 1.0 only
    python universe_builder.py --prescreen-only   # 1.1 only (reads universe_raw.csv)
    python universe_builder.py --no-resume        # ignore checkpoints, fresh run
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

# Reuse SEC primitives already shipped in edgar_fetcher.py
from edgar_fetcher import (
    load_ticker_cik_map,
    http_get_json,
    SEC_SUBMISSIONS_URL,
    SEC_SUBMISSIONS_FILE_URL,
)

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


HERE = Path(__file__).parent
UNIVERSE_RAW_CSV = HERE / "universe_raw.csv"
UNIVERSE_PRESCREENED_CSV = HERE / "universe_prescreened.csv"
UNIVERSE_CHECKPOINT = HERE / ".universe_checkpoint.csv"
UNIVERSE_REV_CHECKPOINT = HERE / ".universe_revenue_checkpoint.csv"
UNIVERSE_SEC_CHECKPOINT = HERE / ".universe_sec_checkpoint.csv"
PRESCREEN_RULES_JSON = HERE / "prescreen_rules.json"

MIN_MARKET_CAP_USD = 1_000_000_000
MIN_ANNUAL_REVENUE_USD = 10_000_000
ACCEPTED_CURRENCIES = {"USD"}

# yfinance exchange-code filter (best-effort).
# fast_info.exchange returns quirky short codes that vary across yfinance
# versions. We use a reject-list approach: known-foreign / OTC / pink-sheet
# codes are rejected; everything else (including empty strings) is accepted,
# because SEC's company_tickers.json already filters to US-reporting issuers.
# Add to EXCLUDED_EXCHANGES as we encounter new codes in real data.
EXCLUDED_EXCHANGES = {
    # OTC / Pink Sheets / Bulletin Board
    'PNK', 'OTC', 'OTCBB', 'OQB', 'OQX', 'PINX',
    # Canada
    'TOR', 'TSX', 'TSXV', 'CVE', 'NEO',
    # UK / Europe
    'LSE', 'LON', 'FRA', 'GER', 'AMS', 'PAR', 'BRU', 'MIL', 'STO',
    # Asia-Pacific
    'HKG', 'HKSE', 'SHH', 'SHG', 'SHA', 'SHZ', 'TYO', 'OSA',
    'KRX', 'KOE', 'SES', 'SGX', 'ASX', 'NZX',
    # Other
    'MEX', 'BMV', 'SAO', 'BVMF', 'JNB', 'JSE', 'TAE', 'IST',
}

# Known US major-exchange codes (documentation only — we use the reject list
# above for actual filtering so future/new US codes aren't accidentally dropped).
KNOWN_US_EXCHANGES = {
    # NASDAQ tiers
    'NMS', 'NGM', 'NCM', 'NGS', 'NASDAQ',
    # NYSE
    'NYQ', 'NYSE',
    # NYSE American (ex-AMEX)
    'ASE', 'AMEX',
    # NYSE Arca
    'PCX', 'ARCA',
    # CBOE BZX (ex-BATS)
    'BTS', 'BATS', 'CBOE',
    # IEX
    'IEX', 'IEXG',
}

# yfinance is rate-sensitive. 0.6s/req ≈ 100/min; for 10k tickers that's
# roughly 100 minutes of wall-clock — designed for overnight runs.
YF_RATE_LIMIT_SLEEP = 0.6
CHECKPOINT_EVERY = 50
REV_CHECKPOINT_EVERY = 25

# SEC EDGAR fair-use: 10 req/s max. 0.15s leaves margin.
SEC_RATE_LIMIT_SLEEP = 0.15
SEC_CHECKPOINT_EVERY = 50

# universe_raw.csv schema — full per-ticker metadata (all fields Phase 1.1 needs).
CSV_FIELDS = [
    'symbol', 'cik', 'name',
    'market_cap', 'currency', 'exchange',
    'avg_dollar_volume_90d',  # from fast_info: three_month_average_volume × last_price
    'annual_revenue',         # from .info: totalRevenue
    'sic', 'sic_description', 'n_10k', 'n_10q',  # from SEC submissions endpoint
]

# Stage 1a checkpoint — fast_info results (mcap + currency + exchange + adv).
CHECKPOINT_FIELDS = [
    'symbol', 'cik', 'name',
    'market_cap', 'currency', 'exchange',
    'avg_dollar_volume_90d',
]

# Stage 1c checkpoint — SEC submissions results.
SEC_CHECKPOINT_FIELDS = ['symbol', 'sic', 'sic_description', 'n_10k', 'n_10q']


# ─── Phase 1.0 helpers ────────────────────────────────────────────────────────

def _get_yf_meta(symbol):
    """Return (mcap, currency, exchange, avg_dollar_volume_90d) via yfinance.

    Uses fast_info (lightweight); any field may be None on failure. We
    deliberately do NOT fall back to the slow `.info` dict at 10k-ticker
    scale — a few thousand Nones are acceptable (they fail the >=$1B
    filter and drop out cleanly).

    `avg_dollar_volume_90d` is computed here from fast_info fields
    (three_month_average_volume × last_price) so Phase 1.1's liquidity
    filter can be pure-local with zero HTTP calls.
    """
    if not HAS_YF:
        return (None, None, None, None)
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        mcap = getattr(fi, 'market_cap', None)
        currency = getattr(fi, 'currency', None)
        exchange = getattr(fi, 'exchange', None)
        vol = getattr(fi, 'three_month_average_volume', None)
        price = getattr(fi, 'last_price', None)
        adv = (float(vol) * float(price)) if (vol and price) else None
        return (
            float(mcap) if mcap else None,
            (currency or None),
            (exchange or None),
            adv,
        )
    except Exception:
        return (None, None, None, None)


def _load_checkpoint():
    """Read previously-fetched rows from the checkpoint CSV, if any.

    If the on-disk schema doesn't match CHECKPOINT_FIELDS (e.g. a pre-Option-B
    checkpoint missing `avg_dollar_volume_90d`), we refuse to reuse it — since
    appending new 7-column rows to an old 6-column file would corrupt the CSV.
    Caller should use --no-resume or delete the file.
    """
    done = {}
    if not UNIVERSE_CHECKPOINT.exists():
        return done
    with UNIVERSE_CHECKPOINT.open('r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        on_disk = list(reader.fieldnames or [])
        if set(on_disk) != set(CHECKPOINT_FIELDS):
            print(f"[1.0] checkpoint schema mismatch: on-disk={on_disk} "
                  f"expected={CHECKPOINT_FIELDS}")
            print(f"[1.0] ignoring stale {UNIVERSE_CHECKPOINT.name} "
                  f"(delete it or pass --no-resume to rebuild).")
            return done
        for row in reader:
            done[row['symbol']] = row
    return done


def _append_checkpoint(rows):
    """Append a batch of rows to the checkpoint CSV (creates header on first run)."""
    first = not UNIVERSE_CHECKPOINT.exists()
    with UNIVERSE_CHECKPOINT.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CHECKPOINT_FIELDS)
        if first:
            writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, '') for k in CHECKPOINT_FIELDS})


def _write_csv(path, rows, fieldnames):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, '') for k in fieldnames})


# ─── Revenue pass (second pass on mcap-filter survivors) ─────────────────────
#
# yfinance .info is slow (~0.8-1s/call), so we run it only after the cheap
# fast_info filter has trimmed the universe from ~10k to ~1500 US $1B+ tickers.

def _get_yf_annual_revenue(symbol):
    """Return most recent annual revenue in USD via yfinance .info.

    Much slower than fast_info — call only on mcap-filter survivors.
    Returns None on any failure; missing data = filter rejection."""
    if not HAS_YF:
        return None
    try:
        t = yf.Ticker(symbol)
        info = t.info
        rev = info.get('totalRevenue')
        return float(rev) if rev else None
    except Exception:
        return None


def _load_revenue_checkpoint():
    done = {}
    if not UNIVERSE_REV_CHECKPOINT.exists():
        return done
    with UNIVERSE_REV_CHECKPOINT.open('r', newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sym = r.get('symbol')
            rev_str = r.get('annual_revenue', '')
            if not sym:
                continue
            try:
                done[sym] = float(rev_str) if rev_str else None
            except (TypeError, ValueError):
                done[sym] = None
    return done


def _append_revenue_checkpoint(rows):
    first = not UNIVERSE_REV_CHECKPOINT.exists()
    with UNIVERSE_REV_CHECKPOINT.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['symbol', 'annual_revenue'])
        if first:
            w.writeheader()
        for sym, rev in rows:
            w.writerow({'symbol': sym, 'annual_revenue': rev if rev is not None else ''})


def _apply_revenue_pass(candidates, resume=True):
    """Second pass: fetch annual revenue for each mcap-filter survivor; reject <$10M.

    Separate checkpoint so a restart doesn't re-fetch revenue for tickers
    we already resolved.
    """
    if not HAS_YF:
        print("[1.0] yfinance unavailable; skipping revenue filter.")
        return candidates

    rev_cache = _load_revenue_checkpoint() if resume else {}
    if rev_cache:
        print(f"[1.0] Revenue checkpoint: {len(rev_cache):,} tickers already resolved")
    elif UNIVERSE_REV_CHECKPOINT.exists() and not resume:
        UNIVERSE_REV_CHECKPOINT.unlink()
        print(f"[1.0] Removed stale revenue checkpoint (--no-resume)")

    pending = [r for r in candidates if r['symbol'] not in rev_cache]
    if pending:
        print(f"[1.0] Revenue pass: fetching annual revenue for {len(pending):,} "
              f"tickers (ETA ~{int(len(pending) * YF_RATE_LIMIT_SLEEP / 60)} min)...")

    batch = []
    t0 = time.time()
    for i, row in enumerate(pending, 1):
        rev = _get_yf_annual_revenue(row['symbol'])
        rev_cache[row['symbol']] = rev
        batch.append((row['symbol'], rev))

        if i % REV_CHECKPOINT_EVERY == 0 or i == len(pending):
            _append_revenue_checkpoint(batch)
            batch = []
            elapsed = max(1e-3, time.time() - t0)
            rate = i / elapsed
            eta_s = (len(pending) - i) / max(0.01, rate)
            print(f"[1.0] revenue {i:>5}/{len(pending)}  rate={rate:.1f}/s  "
                  f"ETA={int(eta_s/60)}m  last={row['symbol']}")

        time.sleep(YF_RATE_LIMIT_SLEEP)

    # Apply revenue filter + annotate each row
    kept = []
    rej_low = 0
    rej_missing = 0
    for row in candidates:
        rev = rev_cache.get(row['symbol'])
        row['annual_revenue'] = rev if rev is not None else ''
        if rev is None:
            rej_missing += 1
            continue
        if rev < MIN_ANNUAL_REVENUE_USD:
            rej_low += 1
            continue
        kept.append(row)

    print(f"[1.0] Revenue filter: kept {len(kept):,}  "
          f"rejected {rej_low:,} (rev<${MIN_ANNUAL_REVENUE_USD/1e6:.0f}M) "
          f"+ {rej_missing:,} (missing)")
    return kept


# ─── SEC submissions pass (Stage 1c — sic + 10-K/10-Q counts) ────────────────
#
# SEC EDGAR submissions endpoint gives us SIC sector code + every filing
# type from the last ~1000. We extract sic, sicDescription, and counts of
# 10-K / 10-Q entries in the `recent.form` array. These fields feed Phase
# 1.1's pure-local filter (SIC exclusion + data-availability gate).

def _count_10k_10q(form_list):
    """Count 10-K and 10-Q entries in a SEC submissions `form` list."""
    n_10k = 0
    n_10q = 0
    for f in form_list or []:
        fu = (f or '').upper().strip()
        if fu == '10-K':
            n_10k += 1
        elif fu == '10-Q':
            n_10q += 1
    return n_10k, n_10q


def _fetch_sec_submission_ext(cik):
    """Fetch (sic, sic_description, n_10k, n_10q) from SEC submissions endpoint.

    Walks `filings.files` pagination to get the TOTAL historical count of
    10-Ks and 10-Qs, not just what lives in the rolling ~1000-filing
    `filings.recent` window.

    Why this matters: for heavy filers (mega-caps that file many 8-Ks,
    proxy statements, S-3/S-8 registrations, insider Forms 3/4/5), the
    1000-filing window can cover only 2-3 years — pushing older 10-Ks/10-Qs
    out of `recent` into paginated files. The unpaginated fetch wrongly
    undercounted GOOGL (4/9), META (2/6), etc. and dropped them from the
    Phase 1.1 prescreen. With pagination, counts reflect true history.

    Returns (None, None, 0, 0) only on the root submissions fetch failure.
    If a paginated file fails to load we keep the partial count and warn —
    degraded but not fatal (most heavy filers still have enough in
    `recent` to clear typical thresholds).
    """
    try:
        doc = http_get_json(SEC_SUBMISSIONS_URL.format(cik=int(cik)))
    except Exception as e:
        print(f"  [warn] submissions fetch failed for CIK {cik}: {e}")
        return (None, None, 0, 0)
    sic = str(doc.get('sic', '') or '').strip() or None
    sic_desc = (doc.get('sicDescription', '') or '').strip() or None

    filings = doc.get('filings', {}) or {}

    # Stage 1: the embedded `recent` window (up to ~1000 filings).
    recent_forms = (filings.get('recent', {}) or {}).get('form', []) or []
    n_10k, n_10q = _count_10k_10q(recent_forms)

    # Stage 2: walk paginated older batches. Each entry in `filings.files`
    # points to an additional JSON at data.sec.gov/submissions/{name}
    # whose root object has the same `form`/`filingDate`/... lists (but
    # NOT wrapped in a filings.recent envelope).
    for entry in filings.get('files', []) or []:
        name = entry.get('name') if isinstance(entry, dict) else None
        if not name:
            continue
        try:
            sub_doc = http_get_json(SEC_SUBMISSIONS_FILE_URL.format(name=name))
        except Exception as e:
            print(f"  [warn] submissions pagination fetch failed for "
                  f"CIK {cik} file={name}: {e}")
            continue
        sub_forms = sub_doc.get('form', []) or []
        sub_10k, sub_10q = _count_10k_10q(sub_forms)
        n_10k += sub_10k
        n_10q += sub_10q

    return (sic, sic_desc, n_10k, n_10q)


def _load_sec_checkpoint():
    done = {}
    if not UNIVERSE_SEC_CHECKPOINT.exists():
        return done
    with UNIVERSE_SEC_CHECKPOINT.open('r', newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sym = r.get('symbol')
            if not sym:
                continue
            try:
                n10k = int(r.get('n_10k') or 0)
            except (TypeError, ValueError):
                n10k = 0
            try:
                n10q = int(r.get('n_10q') or 0)
            except (TypeError, ValueError):
                n10q = 0
            done[sym] = {
                'sic': r.get('sic') or None,
                'sic_description': r.get('sic_description') or None,
                'n_10k': n10k,
                'n_10q': n10q,
            }
    return done


def _append_sec_checkpoint(batch):
    first = not UNIVERSE_SEC_CHECKPOINT.exists()
    with UNIVERSE_SEC_CHECKPOINT.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=SEC_CHECKPOINT_FIELDS)
        if first:
            w.writeheader()
        for r in batch:
            w.writerow({k: r.get(k, '') for k in SEC_CHECKPOINT_FIELDS})


def _apply_sec_pass(candidates, resume=True, refresh_sec=False):
    """Stage 1c: fetch SIC + 10-K/10-Q counts for each Stage-1b survivor.

    Annotates each row with sic, sic_description, n_10k, n_10q. Never
    rejects rows — Phase 1.1 does the actual filtering. Missing fields
    default to empty/0 so the downstream filter can treat them uniformly.

    `refresh_sec`: when True, wipe Stage 1c's checkpoint (keeping 1a/1b
    checkpoints intact). Use this after fixing the SEC fetch logic to
    re-count without blowing away the 100-min Stage 1a yfinance pass.
    """
    if refresh_sec and UNIVERSE_SEC_CHECKPOINT.exists():
        UNIVERSE_SEC_CHECKPOINT.unlink()
        print(f"[1.0] Removed SEC checkpoint (--refresh-sec)")

    sec_cache = _load_sec_checkpoint() if (resume and not refresh_sec) else {}
    if sec_cache:
        print(f"[1.0] SEC checkpoint: {len(sec_cache):,} tickers already resolved")
    elif UNIVERSE_SEC_CHECKPOINT.exists() and not resume:
        UNIVERSE_SEC_CHECKPOINT.unlink()
        print(f"[1.0] Removed stale SEC checkpoint (--no-resume)")

    pending = [r for r in candidates if r['symbol'] not in sec_cache]
    if pending:
        print(f"[1.0] SEC pass: fetching sic + filing counts for {len(pending):,} "
              f"tickers (ETA ~{int(len(pending) * SEC_RATE_LIMIT_SLEEP / 60)} min)...")

    batch = []
    t0 = time.time()
    for i, row in enumerate(pending, 1):
        sic, sic_desc, n10k, n10q = _fetch_sec_submission_ext(row['cik'])
        entry = {
            'symbol': row['symbol'],
            'sic': sic or '',
            'sic_description': sic_desc or '',
            'n_10k': n10k,
            'n_10q': n10q,
        }
        sec_cache[row['symbol']] = {
            'sic': sic, 'sic_description': sic_desc,
            'n_10k': n10k, 'n_10q': n10q,
        }
        batch.append(entry)

        if i % SEC_CHECKPOINT_EVERY == 0 or i == len(pending):
            _append_sec_checkpoint(batch)
            batch = []
            elapsed = max(1e-3, time.time() - t0)
            rate = i / elapsed
            eta_s = (len(pending) - i) / max(0.01, rate)
            print(f"[1.0] sec    {i:>5}/{len(pending)}  rate={rate:.1f}/s  "
                  f"ETA={int(eta_s/60)}m  last={row['symbol']}")

    # Attach sec fields to every candidate (even those already cached)
    missing = 0
    for row in candidates:
        s = sec_cache.get(row['symbol'])
        if not s:
            missing += 1
            row['sic'] = ''
            row['sic_description'] = ''
            row['n_10k'] = 0
            row['n_10q'] = 0
            continue
        row['sic'] = s.get('sic') or ''
        row['sic_description'] = s.get('sic_description') or ''
        row['n_10k'] = s.get('n_10k') or 0
        row['n_10q'] = s.get('n_10q') or 0

    print(f"[1.0] SEC pass: annotated {len(candidates):,} rows  "
          f"(missing sec data: {missing})")
    return candidates


# ─── Phase 1.0 main ───────────────────────────────────────────────────────────

def build_raw_universe(resume=True, refresh_sec=False):
    """Phase 1.0: fetch market caps for SEC-listed tickers, filter to US + >=$1B.

    Writes `universe_raw.csv` sorted by market cap descending.
    Returns the list of kept rows.

    `refresh_sec`: only redo Stage 1c (SEC submissions pass). Stages 1a/1b
    reuse their checkpoints. Useful after fixing SEC fetch logic without
    paying for a full 100-min Stage 1a rebuild.
    """
    if not HAS_YF:
        print("[fatal] yfinance not installed. Run: pip install yfinance")
        sys.exit(1)

    cik_map = load_ticker_cik_map()
    all_tickers = sorted(cik_map.keys())
    print(f"[1.0] SEC master list: {len(all_tickers):,} tickers")

    done = _load_checkpoint() if resume else {}
    if done:
        print(f"[1.0] Resuming — {len(done):,} tickers already in checkpoint")
    elif UNIVERSE_CHECKPOINT.exists() and not resume:
        UNIVERSE_CHECKPOINT.unlink()
        print(f"[1.0] Removed stale checkpoint (--no-resume)")

    pending = [s for s in all_tickers if s not in done]
    print(f"[1.0] Fetching yfinance metadata for {len(pending):,} tickers "
          f"(ETA ~{int(len(pending) * YF_RATE_LIMIT_SLEEP / 60)} min)...")

    batch = []
    t0 = time.time()
    for i, sym in enumerate(pending, 1):
        mcap, currency, exchange, adv = _get_yf_meta(sym)
        row = {
            'symbol': sym,
            'cik': cik_map[sym]['cik'],
            'name': cik_map[sym]['name'],
            'market_cap': mcap if mcap is not None else '',
            'currency': currency or '',
            'exchange': exchange or '',
            'avg_dollar_volume_90d': adv if adv is not None else '',
        }
        batch.append(row)
        done[sym] = row

        if i % CHECKPOINT_EVERY == 0 or i == len(pending):
            _append_checkpoint(batch)
            batch = []
            elapsed = max(1e-3, time.time() - t0)
            rate = i / elapsed
            eta_s = (len(pending) - i) / max(0.01, rate)
            print(f"[1.0] {i:>5}/{len(pending)} done  "
                  f"rate={rate:.1f}/s  ETA={int(eta_s/60)}m  last={sym}")

        time.sleep(YF_RATE_LIMIT_SLEEP)

    # Apply filter
    kept = []
    rejected_counts = {'mcap': 0, 'currency': 0, 'exchange': 0}
    for row in done.values():
        try:
            mcap = float(row.get('market_cap') or 0)
        except (TypeError, ValueError):
            mcap = 0
        currency = (row.get('currency') or '').upper()
        exchange = (row.get('exchange') or '').upper()
        if mcap < MIN_MARKET_CAP_USD:
            rejected_counts['mcap'] += 1
            continue
        if currency not in ACCEPTED_CURRENCIES:
            rejected_counts['currency'] += 1
            continue
        # Best-effort exchange filter: reject only if explicitly foreign / OTC.
        # Empty / unknown codes are accepted (SEC list already filters to US issuers).
        if exchange in EXCLUDED_EXCHANGES:
            rejected_counts['exchange'] += 1
            continue
        kept.append(row)

    print(f"[1.0] Stage-1 filter: kept {len(kept):,}  "
          f"rejected mcap<${MIN_MARKET_CAP_USD/1e9:.0f}B={rejected_counts['mcap']:,}  "
          f"non-USD={rejected_counts['currency']:,}  "
          f"foreign/OTC={rejected_counts['exchange']:,}")

    # Stage 1b: revenue filter (separate yfinance .info pass on survivors)
    kept = _apply_revenue_pass(kept, resume=resume)

    # Stage 1c: SEC submissions pass (sic + 10-K/10-Q counts).
    # Annotation only — no rejection; Phase 1.1 applies the framework filter.
    kept = _apply_sec_pass(kept, resume=resume, refresh_sec=refresh_sec)

    kept.sort(key=lambda r: float(r['market_cap']), reverse=True)
    _write_csv(UNIVERSE_RAW_CSV, kept, CSV_FIELDS)
    print(f"[1.0] Wrote {len(kept):,} rows to {UNIVERSE_RAW_CSV.name} "
          f"(investability filter: mcap>=${MIN_MARKET_CAP_USD/1e9:.0f}B, "
          f"currency=USD, US exchange, annual_revenue>=${MIN_ANNUAL_REVENUE_USD/1e6:.0f}M; "
          f"sic + filing counts annotated for Phase 1.1)")
    return kept


# ─── Phase 1.1 — Prescreen (pure-local filter, no HTTP) ──────────────────────
#
# Reads universe_raw.csv produced by Phase 1.0 and applies a 6-rule filter:
#   A. Liquidity       — avg_dollar_volume_90d >= min_avg_dollar_volume_90d  (row)
#   B. Filing history  — n_10q > min_10q_count_strict_gt (strict)            (row)
#   C. Framework SIC   — sic NOT IN excluded_sic_ranges                       (row)
#   D. SVR sanity      — market_cap / annual_revenue < max_svr                (row)
#   E. Finance cap     — within A+B+C+D survivors whose sic is in
#                        finance_sector_range, keep top-N by annual_revenue;
#                        drop the rest as fail:finance_overflow               (GROUP)
#   F. Dual-class dedup — group survivors by cik; for cik groups with >1 row,
#                        keep the row with highest avg_dollar_volume_90d;
#                        drop the rest as fail:dual_class_secondary          (GROUP)
#                        [Phase 1.7h — prevents GOOGL/GOOG, BRK-A/BRK-B, etc.
#                        from double-counting in sector rank & leader slots]
#
# Rule ordering (E and F are group-level — run after all row-level filters):
#   1. Apply A, B, C, D as row-level filters; tag each row with
#      prescreen_pass_reason ∈ {pass, fail:liquidity, fail:filings, fail:sic=...,
#      fail:svr}.
#   2. Partition intermediate survivors into finance (sic ∈ [6000,6999]) and
#      non-finance.
#   3. Rank finance rows by annual_revenue desc; keep top-N, mark the rest
#      fail:finance_overflow (ties broken by market_cap desc).
#   4. Intermediate output = non_finance survivors ∪ top-N finance survivors.
#   5. Group by cik: within each group, rank by avg_dollar_volume_90d desc;
#      keep row 0, mark the rest fail:dual_class_secondary.
#   6. Final output = dedup survivors.
#
# Excluded SIC ranges (framework incompatible):
#   - 6020-6030 : Banks (net interest margin, not gross margin)
#   - 6798       : REITs (FFO/AFFO, not EPS)
#   - 6199       : Investment funds (other)
#   - 6722       : Mutual funds / unit investment trusts
#   - 6770       : Blank checks / holding shells
#
# Kept in universe (archetype tagging in Phase 1.3 contextualizes verdict):
#   - 6300-6411 : Insurance (BRK, PGR, CB are legitimate leader candidates)
#   - 4911-4939 : Utilities (map cleanly to ARISTOCRAT archetype)
#   - other 6000-6999 non-excluded codes capped at top-N by revenue via Rule E
#
# Edge cases:
#   - annual_revenue null or <=0 → Rule D fails (SVR undefined → exclude).
#   - market_cap null             → Rule D fails.
#   - sic null / blank            → Rule C(i) fails IF `blank_sic_excludes`
#                                   is true (default; see Phase 1.9b note on
#                                   DEFAULT_PRESCREEN_RULES — SEC returns
#                                   blank sic for '40 Act BDCs, and 16/16
#                                   blank-SIC rows in the empirical pool
#                                   were BDCs).
#                                   With the flag off, blank-SIC rows pass
#                                   Rule C and Rule E's 6000-6999 test is
#                                   false → row passes outside finance cap.
#
# No `target_size` cap — natural pool after A–E stands (expected ~1,000-1,200
# from 2,501 raw rows under Phase 1.0).

DEFAULT_PRESCREEN_RULES = {
    "_comment": "Phase 1.1 pure-local 6-rule filter. All fields come from universe_raw.csv.",
    "min_avg_dollar_volume_90d": 5_000_000,
    "min_10q_count_strict_gt": 10,            # Rule B: n_10q > 10 (i.e. >= 11)
    "excluded_sic_ranges": [                  # Rule C
        [6020, 6030],   # Banks
        [6798, 6798],   # REITs
        [6199, 6199],   # Investment funds (other)
        [6722, 6722],   # Mutual funds / UITs
        [6726, 6726],   # Closed-end funds / BDCs (defense-in-depth;
                        # SEC submissions returns blank sic for most
                        # '40 Act filers, so blank_sic_excludes is the
                        # actual effective gate — see below)
        [6770, 6770],   # Blank checks / holding shells
    ],
    # Phase 1.9b (2026-04-19): SEC submissions endpoint genuinely returns
    # sic='' for '40 Act filers (BDCs). Verified 2026-04-19 via
    # diag_bdc_sic.py against ARCC/OBDC/MAIN — all three got blank sic
    # while MSFT anchor returned '7372'. diag_blank_sic.py over the full
    # prescreened pool confirmed 16/16 blank-SIC rows are BDCs (zero
    # false-positive risk). Set false to keep unknown-SIC rows in the
    # pool (pre-1.9b behavior).
    "blank_sic_excludes": True,
    "max_svr": 50,                            # Rule D: market_cap / annual_revenue < 50
    "finance_sector_range": [6000, 6999],     # Rule E window
    "finance_sector_top_n_by_revenue": 50,    # Rule E cap
    "dedup_dual_class_by_cik": True,          # Rule F: keep highest-ADV ticker per CIK
}

PRESCREEN_FIELDS = CSV_FIELDS + ['prescreen_pass_reason']


def _load_prescreen_rules():
    if PRESCREEN_RULES_JSON.exists():
        try:
            return json.loads(PRESCREEN_RULES_JSON.read_text(encoding='utf-8'))
        except Exception as e:
            print(f"[1.1] Failed to parse {PRESCREEN_RULES_JSON.name}: {e}")
    PRESCREEN_RULES_JSON.write_text(
        json.dumps(DEFAULT_PRESCREEN_RULES, indent=2), encoding='utf-8'
    )
    print(f"[1.1] Wrote default rules to {PRESCREEN_RULES_JSON.name} (edit to tune)")
    return DEFAULT_PRESCREEN_RULES


def _sic_is_excluded(sic, excluded_ranges):
    """True if `sic` falls within any [lo, hi] range in excluded_ranges."""
    try:
        s = int(str(sic).strip())
    except (TypeError, ValueError):
        return False
    for rng in excluded_ranges:
        try:
            lo, hi = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            continue
        if lo <= s <= hi:
            return True
    return False


def _as_float(v, default=0.0):
    try:
        return float(v) if v not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _as_int(v, default=0):
    try:
        return int(float(v)) if v not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _sic_in_range(sic, lo, hi):
    """True if `sic` is an int parseable into [lo, hi]. Missing/unparseable → False."""
    try:
        s = int(str(sic).strip())
    except (TypeError, ValueError):
        return False
    return lo <= s <= hi


def _apply_rules(rows, rules):
    """Pure-local 6-rule filter (A liquidity, B filings, C SIC, D SVR,
    E finance cap, F dual-class CIK dedup).

    Mutates each row with a `prescreen_pass_reason` string for transparency:
      "pass"
      "fail:liquidity (adv=$...)"
      "fail:filings (10Q=...)"
      "fail:sic=...."
      "fail:svr (mcap=$..., rev=$..., svr=...)"
      "fail:finance_overflow (rev_rank=..., top_n=...)"
      "fail:dual_class_secondary (cik=..., kept=SYM)"
    Returns only the final-passing rows (A+B+C+D survivors minus E overflow
    minus F dual-class seconds).

    Rule E is group-level and runs AFTER A+B+C+D on surviving rows whose SIC
    falls in finance_sector_range. Tie-break for revenue rank: market_cap desc.

    Rule F (Phase 1.7h) is group-level and runs AFTER E on the E-kept pool.
    Groups rows by `cik`; for CIKs with >1 row (dual-class tickers like
    GOOGL/GOOG, BRK-A/BRK-B), keeps the ticker with highest
    avg_dollar_volume_90d and marks the rest fail:dual_class_secondary.
    Prevents SEC companyfacts being fetched twice per company and stops
    dual classes from double-counting in sector rank / leader selection.
    Disable via rules['dedup_dual_class_by_cik'] = false.
    """
    min_adv = float(rules.get('min_avg_dollar_volume_90d', 5_000_000))
    min_10q_gt = int(rules.get('min_10q_count_strict_gt', 10))
    excluded = rules.get('excluded_sic_ranges', [])
    blank_sic_excludes = bool(rules.get('blank_sic_excludes', False))
    max_svr = float(rules.get('max_svr', 50))
    fin_lo, fin_hi = rules.get('finance_sector_range', [6000, 6999])
    fin_lo, fin_hi = int(fin_lo), int(fin_hi)
    fin_top_n = int(rules.get('finance_sector_top_n_by_revenue', 50))
    dedup_dual_class = bool(rules.get('dedup_dual_class_by_cik', True))

    # ── Row-level pass: A, B, C, D ──────────────────────────────────────────
    abcd_survivors = []
    fail = {'liquidity': 0, 'filings': 0, 'sic': 0, 'svr': 0}
    for row in rows:
        adv = _as_float(row.get('avg_dollar_volume_90d'))
        n_10q = _as_int(row.get('n_10q'))
        sic = (row.get('sic') or '').strip()
        mcap = _as_float(row.get('market_cap'))
        rev = _as_float(row.get('annual_revenue'))

        # A. Liquidity
        if adv < min_adv:
            row['prescreen_pass_reason'] = f"fail:liquidity (adv=${int(adv):,})"
            fail['liquidity'] += 1
            continue
        # B. Filings (strict >)
        if n_10q <= min_10q_gt:
            row['prescreen_pass_reason'] = f"fail:filings (10Q={n_10q})"
            fail['filings'] += 1
            continue
        # C(i). Blank SIC — SEC submissions returns '' for '40 Act filers
        #        (BDCs). Empirically every blank-SIC row in the pool is a
        #        BDC (verified 2026-04-19 via diag_blank_sic.py: 16/16 were
        #        BDCs including those missed by our name heuristic like
        #        HTGC / CSWC / OTF / MFIC). BDCs are '40 Act pass-through
        #        vehicles — framework-incompatible for the same reason as
        #        closed-end funds (SIC 6726). Disable via
        #        rules['blank_sic_excludes'] = false.
        if not sic and blank_sic_excludes:
            row['prescreen_pass_reason'] = (
                "fail:sic=blank (likely BDC / '40 Act filer)"
            )
            fail['sic'] += 1
            continue
        # C(ii). Framework SIC exclusion (explicit range match)
        if _sic_is_excluded(sic, excluded):
            row['prescreen_pass_reason'] = f"fail:sic={sic or 'missing'}"
            fail['sic'] += 1
            continue
        # D. SVR sanity (market_cap / annual_revenue < max_svr)
        if mcap <= 0 or rev <= 0:
            row['prescreen_pass_reason'] = (
                f"fail:svr (mcap=${int(mcap):,}, rev=${int(rev):,}, svr=undef)"
            )
            fail['svr'] += 1
            continue
        svr = mcap / rev
        if svr >= max_svr:
            row['prescreen_pass_reason'] = (
                f"fail:svr (mcap=${int(mcap):,}, rev=${int(rev):,}, svr={svr:.1f})"
            )
            fail['svr'] += 1
            continue

        abcd_survivors.append(row)

    # ── Group-level pass: E (finance-sector top-N cap by revenue) ───────────
    finance, non_finance = [], []
    for row in abcd_survivors:
        if _sic_in_range(row.get('sic'), fin_lo, fin_hi):
            finance.append(row)
        else:
            non_finance.append(row)

    # Rank finance rows by annual_revenue desc (tie-break: market_cap desc)
    finance.sort(
        key=lambda r: (_as_float(r.get('annual_revenue')),
                       _as_float(r.get('market_cap'))),
        reverse=True,
    )
    finance_kept = finance[:fin_top_n]
    finance_overflow = finance[fin_top_n:]
    for i, r in enumerate(finance_overflow, start=fin_top_n + 1):
        r['prescreen_pass_reason'] = (
            f"fail:finance_overflow (rev_rank={i}, top_n={fin_top_n})"
        )

    # Tag final-kept rows
    for r in non_finance:
        r['prescreen_pass_reason'] = 'pass'
    for r in finance_kept:
        r['prescreen_pass_reason'] = 'pass'

    e_kept = non_finance + finance_kept
    e_kept.sort(key=lambda r: _as_float(r.get('market_cap')), reverse=True)

    # ── Group-level pass: F (dual-class CIK dedup) ──────────────────────────
    # Phase 1.7h — keep highest-ADV ticker per CIK. Dual-class pairs
    # (GOOGL/GOOG, BRK-A/BRK-B, FOXA/FOX, etc.) share one CIK: without
    # dedup they double-count in sector rank, poison leader selection,
    # and cause edgar_fetcher to hit the same SEC endpoint twice.
    dual_class_dropped = []
    if dedup_dual_class:
        by_cik = {}
        no_cik = []
        for r in e_kept:
            cik = (r.get('cik') or '').strip()
            if not cik:
                no_cik.append(r)
                continue
            by_cik.setdefault(cik, []).append(r)

        kept = list(no_cik)
        for cik, group in by_cik.items():
            if len(group) == 1:
                kept.append(group[0])
                continue
            # Sort by avg_dollar_volume_90d desc; tie-break by market_cap desc
            group.sort(
                key=lambda r: (_as_float(r.get('avg_dollar_volume_90d')),
                               _as_float(r.get('market_cap'))),
                reverse=True,
            )
            winner = group[0]
            kept.append(winner)
            for loser in group[1:]:
                loser['prescreen_pass_reason'] = (
                    f"fail:dual_class_secondary "
                    f"(cik={cik}, kept={winner.get('symbol', '?')})"
                )
                dual_class_dropped.append(loser)
        kept.sort(key=lambda r: _as_float(r.get('market_cap')), reverse=True)
    else:
        kept = e_kept

    print(
        f"[1.1] Prescreen funnel: {len(rows):,} in  "
        f"-> A {len(rows) - fail['liquidity']:,}  "
        f"-> A+B {len(rows) - fail['liquidity'] - fail['filings']:,}  "
        f"-> A+B+C {len(rows) - sum(fail[k] for k in ('liquidity', 'filings', 'sic')):,}  "
        f"-> A+B+C+D {len(abcd_survivors):,}  "
        f"-> after E {len(e_kept):,}  "
        f"-> final after F {len(kept):,}"
    )
    print(
        f"[1.1] Rejects: liquidity={fail['liquidity']:,}, "
        f"filings={fail['filings']:,}, sic={fail['sic']:,}, "
        f"svr={fail['svr']:,}, finance_overflow={len(finance_overflow):,}, "
        f"dual_class_secondary={len(dual_class_dropped):,}"
    )
    print(
        f"[1.1] Finance super-sector [{fin_lo}-{fin_hi}]: "
        f"{len(finance):,} survivors A+B+C+D -> kept top {len(finance_kept):,} by revenue"
    )
    if dual_class_dropped:
        dropped_syms = ','.join(sorted(r.get('symbol', '?') for r in dual_class_dropped))
        print(f"[1.1] Dual-class dedup: dropped {len(dual_class_dropped)} "
              f"secondary classes: {dropped_syms}")
    return kept


def prescreen_universe():
    """Phase 1.1: pure-local filter → `universe_prescreened.csv`.

    Reads universe_raw.csv (from Phase 1.0), applies prescreen rules locally,
    writes ~500 rows to universe_prescreened.csv. Zero HTTP calls — safe to
    re-run any time to retune thresholds.
    """
    if not UNIVERSE_RAW_CSV.exists():
        print(f"[1.1] {UNIVERSE_RAW_CSV.name} not found. Run --raw-only first.")
        sys.exit(1)

    rules = _load_prescreen_rules()

    with UNIVERSE_RAW_CSV.open('r', newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    print(f"[1.1] Loaded {len(rows):,} rows from {UNIVERSE_RAW_CSV.name}")

    kept = _apply_rules(rows, rules)
    _write_csv(UNIVERSE_PRESCREENED_CSV, kept, PRESCREEN_FIELDS)
    fin_lo, fin_hi = rules.get('finance_sector_range', [6000, 6999])
    dedup_on = bool(rules.get('dedup_dual_class_by_cik', True))
    print(
        f"[1.1] Wrote {len(kept):,} rows to {UNIVERSE_PRESCREENED_CSV.name} "
        f"(A liquidity>=${int(rules.get('min_avg_dollar_volume_90d', 0))/1e6:.0f}M, "
        f"B n_10q>{rules.get('min_10q_count_strict_gt')}, "
        f"C sic∉excluded, "
        f"D svr<{rules.get('max_svr')}, "
        f"E finance[{fin_lo}-{fin_hi}] top-{rules.get('finance_sector_top_n_by_revenue')}, "
        f"F dual_class_dedup={'on' if dedup_on else 'off'})"
    )
    return kept


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Build Quantfolio universe (Layer 1 phases 1.0 + 1.1)."
    )
    p.add_argument("--build", action="store_true",
                   help="Run phases 1.0 + 1.1 end-to-end")
    p.add_argument("--raw-only", action="store_true",
                   help="Phase 1.0 only (SEC ticker fetch + mcap filter)")
    p.add_argument("--prescreen-only", action="store_true",
                   help="Phase 1.1 only (requires existing universe_raw.csv)")
    p.add_argument("--no-resume", action="store_true",
                   help="Ignore checkpoint and restart Phase 1.0 from scratch")
    p.add_argument("--refresh-sec", action="store_true",
                   help="Redo only Stage 1c (SEC submissions). Keeps Stage 1a/1b "
                        "checkpoints intact — useful after fixing SEC fetch logic "
                        "without paying for a full 100-min Stage 1a rebuild.")
    args = p.parse_args()

    if not (args.build or args.raw_only or args.prescreen_only):
        p.print_help()
        return

    if args.raw_only or args.build:
        build_raw_universe(
            resume=not args.no_resume,
            refresh_sec=args.refresh_sec,
        )
    if args.prescreen_only or args.build:
        prescreen_universe()


if __name__ == "__main__":
    main()
