"""
fundamental_metrics.py
──────────────────────
Computes the 15 Good Firm Framework metrics from raw XBRL facts cached
in fundamentals.db by edgar_fetcher.py.

Public entry point: `compute_metrics(symbol, sector_context=None)`
Returns a flat dict with all 15 values (None if unavailable).

Called by fundamental_screener.py and by the /api/screener endpoints.
"""
import argparse
import json
from datetime import datetime, timedelta

from edgar_fetcher import get_facts, get_ticker_info, get_db

# yfinance is already a Quantfolio dependency — use it for live market cap.
try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


# ─── Date / period helpers ────────────────────────────────────────────────────

def _parse_date(d):
    if not d:
        return None
    try:
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _period_days(fact):
    s = _parse_date(fact.get('start_date'))
    e = _parse_date(fact.get('end_date'))
    if s is None or e is None:
        return None
    return (e - s).days


def _classify_period(fact):
    """Label a duration fact by its window length."""
    days = _period_days(fact)
    if days is None:
        return None
    if 330 <= days <= 400:
        return 'annual'
    if 260 <= days <= 290:
        return 'ytd_9mo'
    if 170 <= days <= 200:
        return 'ytd_6mo'
    if 80 <= days <= 100:
        return 'quarter'
    return 'other'


# ─── Fact retrieval wrappers ─────────────────────────────────────────────────

def _duration_facts(symbol, metric, conn=None):
    return [f for f in get_facts(symbol, metric, conn=conn) if f['period_type'] == 'duration']


def _instant_facts(symbol, metric, conn=None):
    return [f for f in get_facts(symbol, metric, conn=conn) if f['period_type'] == 'instant']


# ─── TTM / FY extractors ─────────────────────────────────────────────────────

def latest_ttm_fact(symbol, metric, conn=None):
    """
    Return the most recent ~12-month total for an income/cash-flow metric
    AS A DICT with both the value and the end_date of the last contributing
    window, so callers can apply staleness checks.

      {'value': <float>, 'end_date': 'YYYY-MM-DD'}   or   None

    Priority (same as latest_ttm):
      1. Any duration fact of ~annual length (330–400 days).
      2. Latest 10-K FY + latest 10-Q YTD − prior-year same-YTD.
      3. Sum of last 4 non-overlapping quarter-alone facts.

    Use `latest_ttm()` if you only need the value; this is the one to call
    when you need to reason about freshness (e.g. Phase 1.7d stale-GP bug,
    where COST's GrossProfit tag stopped filing after FY2019 and produced
    a bogus ratio when divided by current Revenue).
    """
    facts = _duration_facts(symbol, metric, conn=conn)
    if not facts:
        return None

    # (1) Most recent annual-window fact
    annual = [f for f in facts if _classify_period(f) == 'annual']
    if annual:
        return {'value': annual[0]['value'], 'end_date': annual[0]['end_date']}

    # (2) 10-K FY + YTD rollover
    fy_facts = [f for f in facts
                if f.get('fiscal_period') == 'FY' and f.get('form') == '10-K']
    ytd_facts = [f for f in facts
                 if f.get('form') == '10-Q'
                 and _classify_period(f) in ('ytd_6mo', 'ytd_9mo', 'quarter')]
    if fy_facts and ytd_facts:
        fy = fy_facts[0]
        fy_end = _parse_date(fy['end_date'])
        latest_q = ytd_facts[0]
        latest_q_end = _parse_date(latest_q['end_date'])
        if fy_end and latest_q_end and latest_q_end > fy_end:
            latest_period = _classify_period(latest_q)
            prior_candidates = [
                f for f in ytd_facts
                if _classify_period(f) == latest_period
                and _parse_date(f['end_date'])
                and _parse_date(f['end_date']) <= fy_end
            ]
            if prior_candidates:
                return {
                    'value': fy['value'] + latest_q['value']
                             - prior_candidates[0]['value'],
                    'end_date': latest_q['end_date'],
                }
            return {
                'value': fy['value'] + latest_q['value'],
                'end_date': latest_q['end_date'],
            }

    # (3) Sum of last 4 quarter-alone facts
    q_facts = [f for f in facts if _classify_period(f) == 'quarter']
    seen = set()
    chosen = []
    for f in q_facts:
        if f['end_date'] in seen:
            continue
        seen.add(f['end_date'])
        chosen.append(f)
        if len(chosen) == 4:
            break
    if len(chosen) == 4:
        return {
            'value': sum(f['value'] for f in chosen),
            'end_date': chosen[0]['end_date'],
        }

    # Last resort — any single duration fact
    return {'value': facts[0]['value'], 'end_date': facts[0]['end_date']}


def latest_ttm(symbol, metric, conn=None):
    """Thin wrapper — just the value. Use latest_ttm_fact() when you also
    need the end_date (for staleness checks)."""
    fact = latest_ttm_fact(symbol, metric, conn=conn)
    return fact['value'] if fact else None


def _is_ttm_fresh(fact, ref_end_date, max_lag_days=450):
    """True if `fact`'s end_date is within `max_lag_days` of `ref_end_date`.

    Returns True if fact or ref_end_date is missing (can't verify staleness
    — fall back to default trust). Returns True if the fact's window ENDS
    AFTER the reference (fact newer than reference). Returns False only
    when we can confirm the fact ended > max_lag_days before the reference.

    Used for Phase 1.7d: when Revenue reports FY2025-08-31 but GrossProfit's
    latest value is from 2019-09-01, that's ~2,190 days stale — rejected.
    Default 450-day tolerance covers one full fiscal year plus a 3-month
    filing cushion.
    """
    if not fact or not ref_end_date:
        return fact is not None
    f_end = _parse_date(fact.get('end_date'))
    r_end = _parse_date(ref_end_date)
    if not f_end or not r_end:
        return True
    return (r_end - f_end).days <= max_lag_days


def ttm_at_offset(symbol, metric, quarters_back=4, conn=None):
    """TTM ending ~N quarters before the latest (for YoY comparison)."""
    facts = _duration_facts(symbol, metric, conn=conn)
    if not facts:
        return None
    annual = [f for f in facts if _classify_period(f) == 'annual']
    if quarters_back == 4 and len(annual) >= 2:
        return annual[1]['value']  # second-most-recent annual = prior TTM
    q_facts = [f for f in facts if _classify_period(f) == 'quarter']
    if len(q_facts) >= quarters_back + 4:
        return sum(f['value'] for f in q_facts[quarters_back:quarters_back + 4])
    return None


def fy_series(symbol, metric, years=5, conn=None):
    """List of (fiscal_year, value) for the last N fiscal years, newest first.

    Dedups by `end_date` (NOT by the DB's `fiscal_year` column). The `fy` field
    in SEC companyfacts is the *filing's* fiscal year, not the fact's own year
    — a single 10-K with 3 years of comparatives tags every fact with the
    filing's fy, so deduping by `fiscal_year` collapses all comparatives from
    one filing into a single row. GOOGL hit this (2 10-Ks in DB → only 2 rows
    → `shares_growth_3y` returned None). NVDA/WMT hit a related failure mode:
    with ≥4 10-Ks in the DB, fy_series returned 4 rows but each came from a
    different filing, mixing pre-split values (old filings) with post-split
    values (new filings) — producing bogus 800%+ dilution flags.

    Fix: dedup by `end_date`, and within each end_date pick the fact with the
    max `accession` (monotonic per-symbol). That's the latest-filed value →
    every value lands in the current reporting basis (e.g. 10:1 split gets
    restated across ALL comparative years). Phase 1.7g.
    """
    facts = _duration_facts(symbol, metric, conn=conn)
    fy_facts = [f for f in facts
                if f.get('fiscal_period') == 'FY'
                and f.get('form') == '10-K'
                and _classify_period(f) in ('annual', None)]

    # Pick the max-accession fact per end_date (latest restatement wins).
    by_end = {}
    for f in fy_facts:
        end = f.get('end_date')
        if not end:
            continue
        acc = f.get('accession') or ''
        prev = by_end.get(end)
        if prev is None or (prev.get('accession') or '') < acc:
            by_end[end] = f

    # Newest fiscal-year-end first.
    sorted_facts = sorted(by_end.values(),
                          key=lambda f: f.get('end_date') or '',
                          reverse=True)

    out = []
    for f in sorted_facts[:years]:
        end = f.get('end_date') or ''
        # Prefer the end_date's calendar year as the label — more reliable
        # than the DB's `fiscal_year` (filing's fy). Falls back to the DB
        # value only if end_date is malformed.
        if len(end) >= 4 and end[:4].isdigit():
            fy_label = int(end[:4])
        else:
            fy_label = f.get('fiscal_year')
        out.append((fy_label, f['value']))
    return out


def latest_instant(symbol, metric, conn=None):
    rows = _instant_facts(symbol, metric, conn=conn)
    return rows[0]['value'] if rows else None


def instant_at_offset(symbol, metric, years_back=3, conn=None):
    """Instant value closest to (today - years_back * 365)."""
    rows = _instant_facts(symbol, metric, conn=conn)
    if not rows:
        return None
    target = datetime.now().date() - timedelta(days=years_back * 365)
    best, best_diff = None, None
    for r in rows:
        d = _parse_date(r['end_date'])
        if d is None:
            continue
        diff = abs((d - target).days)
        if best_diff is None or diff < best_diff:
            best, best_diff = r, diff
    return best['value'] if best else None


# ─── Trajectory ──────────────────────────────────────────────────────────────

def quarterly_revenue_yoy_series(symbol, n=8, conn=None):
    """The last N quarterly YoY growth rates, newest first."""
    q_facts = [f for f in _duration_facts(symbol, 'Revenue', conn=conn)
               if _classify_period(f) == 'quarter']
    seen = set()
    clean = []
    for f in q_facts:
        if f['end_date'] in seen:
            continue
        seen.add(f['end_date'])
        d = _parse_date(f['end_date'])
        if d is None:
            continue
        clean.append({**f, '_end': d})
    clean.sort(key=lambda f: f['_end'], reverse=True)

    yoy = []
    for cur in clean[:n]:
        target_end = cur['_end'] - timedelta(days=365)
        prior = next(
            (p for p in clean
             if p['_end'] < cur['_end']
             and abs((p['_end'] - target_end).days) <= 45),
            None
        )
        if prior and prior['value']:
            yoy.append(cur['value'] / prior['value'] - 1.0)
    return yoy


def classify_trajectory(yoy_series):
    """Categorize recent revenue trend: accelerating / stable / decelerating."""
    if len(yoy_series) < 4:
        return None
    recent = sum(yoy_series[:2]) / 2.0
    prior = sum(yoy_series[2:4]) / 2.0
    delta = recent - prior
    if delta > 0.02:
        return 'accelerating'
    if delta < -0.02:
        return 'decelerating'
    return 'stable'


# ─── Market cap (yfinance) ───────────────────────────────────────────────────

_mcap_cache = {}

def get_market_cap(symbol):
    """Fetch market cap in USD. In-memory cached per run."""
    if symbol in _mcap_cache:
        return _mcap_cache[symbol]
    if not HAS_YF:
        _mcap_cache[symbol] = None
        return None
    try:
        t = yf.Ticker(symbol)
        mcap = None
        try:
            fi = t.fast_info
            mcap = getattr(fi, 'market_cap', None)
        except Exception:
            pass
        if not mcap:
            try:
                mcap = t.info.get('marketCap')
            except Exception:
                mcap = None
        _mcap_cache[symbol] = float(mcap) if mcap else None
    except Exception:
        _mcap_cache[symbol] = None
    return _mcap_cache[symbol]


_div_yield_cache = {}

def get_dividend_yield(symbol):
    """Fetch current dividend yield as a decimal (0.025 = 2.5%).

    Returns None if yfinance is unavailable, the ticker pays no dividend,
    or the lookup fails. Normalizes across yfinance versions that sometimes
    return percent (2.5) vs decimal (0.025)."""
    if symbol in _div_yield_cache:
        return _div_yield_cache[symbol]
    if not HAS_YF:
        _div_yield_cache[symbol] = None
        return None
    try:
        t = yf.Ticker(symbol)
        dy = None
        try:
            dy = t.info.get('dividendYield')
        except Exception:
            pass
        if dy is not None:
            dy = float(dy)
            # Some yfinance versions return percent (e.g. 2.5), others decimal (0.025).
            # Normalize to decimal.
            if dy > 1.0:
                dy = dy / 100.0
        _div_yield_cache[symbol] = dy if dy else None
    except Exception:
        _div_yield_cache[symbol] = None
    return _div_yield_cache[symbol]


# ─── Main: compute 15 metrics ────────────────────────────────────────────────

def compute_metrics(symbol, sector_context=None, conn=None):
    """
    Compute the 15 Good Firm Framework metrics for a ticker.

    `sector_context` (optional, filled in by the screener cross-cutting pass):
        {'sector_svr_median': float, 'sector_rank': int, 'sector_size': int}

    Returns a flat dict; missing data → None. Safe for ETFs/ADRs (returns the
    empty shell with flag_spac_or_microcap filled from market cap alone).
    """
    symbol = symbol.upper().strip()
    info = get_ticker_info(symbol, conn=conn)
    if info is None or info.get('status') != 'ok':
        return _empty_metrics(symbol, info)

    # ── Revenue & growth ──
    rev_ttm = latest_ttm(symbol, 'Revenue', conn=conn)
    rev_prior = ttm_at_offset(symbol, 'Revenue', 4, conn=conn)
    rev_yoy = (rev_ttm / rev_prior - 1.0) if (rev_ttm and rev_prior and rev_prior > 0) else None

    fy_rev = fy_series(symbol, 'Revenue', years=5, conn=conn)
    rev_3y_cagr = None
    if len(fy_rev) >= 4:
        latest, old = fy_rev[0][1], fy_rev[3][1]
        if old and old > 0 and latest:
            rev_3y_cagr = (latest / old) ** (1 / 3.0) - 1.0

    yoy_hist = quarterly_revenue_yoy_series(symbol, n=8, conn=conn)
    trajectory = classify_trajectory(yoy_hist)

    # ── Margins ──
    # Phase 1.7d: staleness-gated extraction. Some issuers stop filing
    # GrossProfit / CostOfRevenue tags after a schema change (e.g. COST
    # after FY2019, ORCL after FY2018/2011). The raw latest_ttm() picks up
    # the stale value; dividing by a current Revenue yields a bogus ratio.
    # Here we anchor both GP and CoR freshness to the Revenue window end.
    rev_fact = latest_ttm_fact(symbol, 'Revenue', conn=conn)
    rev_end = rev_fact['end_date'] if rev_fact else None

    gp_fact = latest_ttm_fact(symbol, 'GrossProfit', conn=conn)
    cor_fact = latest_ttm_fact(symbol, 'CostOfRevenue', conn=conn)
    gp_ttm = gp_fact['value'] if _is_ttm_fresh(gp_fact, rev_end) else None
    cor_ttm = cor_fact['value'] if _is_ttm_fresh(cor_fact, rev_end) else None

    if gp_ttm is None and rev_ttm is not None and cor_ttm is not None:
        gp_ttm = rev_ttm - cor_ttm
    gross_margin = (gp_ttm / rev_ttm) if (gp_ttm is not None and rev_ttm and rev_ttm > 0) else None

    op_inc_ttm = latest_ttm(symbol, 'OperatingIncomeLoss', conn=conn)
    op_margin = (op_inc_ttm / rev_ttm) if (op_inc_ttm is not None and rev_ttm and rev_ttm > 0) else None

    # ── Cash flow ──
    ocf_ttm = latest_ttm(symbol, 'OperatingCashFlow', conn=conn)
    capex_ttm = latest_ttm(symbol, 'CapEx', conn=conn)
    fcf_ttm = None
    if ocf_ttm is not None:
        fcf_ttm = ocf_ttm - (capex_ttm or 0)
    fcf_margin = (fcf_ttm / rev_ttm) if (fcf_ttm is not None and rev_ttm and rev_ttm > 0) else None

    rule_40 = None
    if rev_yoy is not None and fcf_margin is not None:
        rule_40 = (rev_yoy + fcf_margin) * 100.0

    # ── Moat proxy: ROIC ──
    assets = latest_instant(symbol, 'Assets', conn=conn)
    cur_liab = latest_instant(symbol, 'LiabilitiesCurrent', conn=conn) or 0
    cash = latest_instant(symbol, 'Cash', conn=conn) or 0
    roic_ttm = None
    if op_inc_ttm is not None and assets:
        invested = assets - cur_liab - cash
        if invested and invested > 0:
            nopat = op_inc_ttm * 0.79  # ~21% effective tax
            roic_ttm = nopat / invested

    # ── Market cap + SVR ──
    mcap = get_market_cap(symbol)
    div_yield = get_dividend_yield(symbol)
    svr = (mcap / rev_ttm) if (mcap and rev_ttm and rev_ttm > 0) else None

    sector_svr_median = (sector_context or {}).get('sector_svr_median')
    sector_rank = (sector_context or {}).get('sector_rank')
    svr_vs_sector = (svr / sector_svr_median) if (svr and sector_svr_median) else None

    # ── Dealbreaker flags ──
    # Phase 1.7c: dilution check uses `WeightedAverageSharesBasic` (annual
    # duration facts from 10-K) instead of the `SharesOutstanding` instant
    # point-in-time metric. Weighted-average shares are natively split-
    # adjusted by the filer — NVDA's 10:1 split in Jun 2024 and TSLA's 3:1
    # in Aug 2022 previously showed as 885%/18% bogus "dilution" with the
    # instant metric. The weighted-average denominator already divides by
    # the split ratio, so cross-split comparisons are meaningful.
    shares_series = fy_series(symbol, 'WeightedAverageSharesBasic', years=4, conn=conn)
    shares_now = shares_series[0][1] if len(shares_series) >= 1 else None
    shares_3y_ago = shares_series[3][1] if len(shares_series) >= 4 else None
    shares_growth_3y = None
    flag_diluting = None
    if shares_now and shares_3y_ago and shares_3y_ago > 0:
        shares_growth_3y = shares_now / shares_3y_ago - 1.0
        flag_diluting = shares_growth_3y > 0.15

    flag_burning_cash = (ocf_ttm < 0) if (ocf_ttm is not None) else None
    flag_spac_or_microcap = (mcap < 500_000_000) if (mcap is not None) else None

    return {
        'symbol': symbol,
        'name': info.get('name'),
        'sector': info.get('sic_description'),
        'sic': info.get('sic'),
        'market_cap': mcap,
        'dividend_yield': div_yield,
        # Growth
        'revenue_ttm': rev_ttm,
        'revenue_yoy_growth': rev_yoy,
        'revenue_3y_cagr': rev_3y_cagr,
        'growth_trajectory': trajectory,
        # Business Model
        'gross_margin_ttm': gross_margin,
        'operating_margin_ttm': op_margin,
        # Profitability
        'operating_cash_flow_ttm': ocf_ttm,
        'free_cash_flow_ttm': fcf_ttm,
        'fcf_margin_ttm': fcf_margin,
        'rule_40_score': rule_40,
        # Moat
        'market_cap_rank_in_sector': sector_rank,
        'roic_ttm': roic_ttm,
        # Valuation
        'svr': svr,
        'svr_vs_sector_median': svr_vs_sector,
        # Dealbreaker flags
        'flag_diluting': flag_diluting,
        'shares_growth_3y': shares_growth_3y,
        'flag_burning_cash': flag_burning_cash,
        'flag_spac_or_microcap': flag_spac_or_microcap,
        # Metadata
        'data_status': info.get('status'),
        'data_fetched_at': info.get('last_fetched'),
    }


def _empty_metrics(symbol, info):
    """Empty-shell dict for tickers with no SEC data (ETFs, ADRs)."""
    mcap = get_market_cap(symbol)
    div_yield = get_dividend_yield(symbol)
    return {
        'symbol': symbol,
        'name': (info or {}).get('name'),
        'sector': None,
        'sic': None,
        'market_cap': mcap,
        'dividend_yield': div_yield,
        'revenue_ttm': None,
        'revenue_yoy_growth': None,
        'revenue_3y_cagr': None,
        'growth_trajectory': None,
        'gross_margin_ttm': None,
        'operating_margin_ttm': None,
        'operating_cash_flow_ttm': None,
        'free_cash_flow_ttm': None,
        'fcf_margin_ttm': None,
        'rule_40_score': None,
        'market_cap_rank_in_sector': None,
        'roic_ttm': None,
        'svr': None,
        'svr_vs_sector_median': None,
        'flag_diluting': None,
        'shares_growth_3y': None,
        'flag_burning_cash': None,
        'flag_spac_or_microcap': (mcap < 500_000_000) if mcap else None,
        'data_status': (info or {}).get('status', 'unknown'),
        'data_fetched_at': (info or {}).get('last_fetched'),
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Compute Good Firm metrics for one ticker")
    p.add_argument("--ticker", required=True)
    args = p.parse_args()
    print(json.dumps(compute_metrics(args.ticker), indent=2, default=str))
