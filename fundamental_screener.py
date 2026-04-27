"""
fundamental_screener.py
───────────────────────
Phase 1.9 — 2-archetype rubric. Each ticker is classified as MATURE or
GROWTH on a binary Revenue-YoY split (threshold `CLASSIFIER_YOY_THRESHOLD`,
locked at 12% per `diag_threshold_sensitivity.py` 2026-04-18); per-archetype
5-test rubrics + per-archetype dealbreakers then produce a 4-verdict schema:

    LEADER  — 5/5 tests + rank ≤ 5 in sector + no dealbreaker
    GEM     — 5/5 tests + rank > 5 + no dealbreaker
    WATCH   — 3–4/5 tests + no dealbreaker
    AVOID   — ≤ 2/5 tests OR any dealbreaker
    INSUFFICIENT_DATA — < 3 tests have non-None result (ETFs, ingestion gaps)

Public entry points:
    score_ticker(metrics)          → metrics dict enriched with tests/flags/score/verdict
    run_full_screen(symbols)       → list of scored dicts (sector context applied)

Called by /api/screener endpoints.
"""
import argparse
import csv
import json
from pathlib import Path
from statistics import median

from classifier import classify
from fundamental_metrics import compute_metrics
from edgar_fetcher import load_tickers_from_csv, get_db, _load_universe_symbols


# ─── Archetype classifier ─────────────────────────────────────────────────────
# Phase 1.9: binary Revenue-YoY split. 12% is the user-locked threshold
# (diag_threshold_sensitivity.py 2026-04-18): best anchor correctness,
# balanced population (~68/32 MATURE/GROWTH), and projected leader pool
# of ~160 total. See plan file Phase 1.9 for rationale.
CLASSIFIER_YOY_THRESHOLD = 0.12


def classify_archetype(m):
    """Return {'MATURE', 'GROWTH', 'UNKNOWN'}.

    Rev-YoY < T  → MATURE  (stable, cash-throwing businesses)
    Rev-YoY ≥ T  → GROWTH  (fast-growers; held to efficient-frontier bars)
    Rev-YoY None → UNKNOWN (ETFs, ADRs, ingestion gaps — no rubric applied)
    """
    g = m.get('revenue_yoy_growth')
    if g is None:
        return 'UNKNOWN'
    return 'GROWTH' if g >= CLASSIFIER_YOY_THRESHOLD else 'MATURE'


# ─── MATURE rubric (5 tests) ──────────────────────────────────────────────────
# Reward stable profitability + cash throw-off. Don't punish slow growth —
# only punish *shrinking* (3y CAGR < 0).

def _test_mature_not_declining(m):
    """Pass if 3y revenue CAGR ≥ 0% (not shrinking over the medium term)."""
    cagr = m.get('revenue_3y_cagr')
    if cagr is None:
        return None
    return cagr >= 0.0


def _test_mature_margin_quality(m):
    """Pass if operating margin ≥ 10% (real operating leverage)."""
    om = m.get('operating_margin_ttm')
    if om is None:
        return None
    return om >= 0.10


def _test_mature_cash_generation(m):
    """Pass if OCF > 0 AND FCF margin ≥ 8% (the core MATURE thesis)."""
    ocf = m.get('operating_cash_flow_ttm')
    fcf_m = m.get('fcf_margin_ttm')
    if ocf is None or fcf_m is None:
        return None
    return (ocf > 0) and (fcf_m >= 0.08)


def _test_mature_moat(m):
    """Pass if ROIC ≥ 10% OR top-5 by market cap in sector.

    Softened from Path-A's top-2 per user 2026-04-18 ("rank ≤ 5 is kinder
    to #3-5 sector leaders"). Shared with GROWTH rubric.
    """
    roic = m.get('roic_ttm')
    rank = m.get('market_cap_rank_in_sector')
    if roic is None and rank is None:
        return None
    if roic is not None and roic >= 0.10:
        return True
    if rank is not None and rank <= 5:
        return True
    return False


def _test_mature_stability(m):
    """Pass if trajectory is NOT decelerating AND NOT diluting (3y share
    count). Rewards consistency + capital discipline."""
    trj = m.get('growth_trajectory')
    flag_dil = m.get('flag_diluting')
    if trj is None and flag_dil is None:
        return None
    if trj == 'decelerating':
        return False
    if flag_dil is True:
        return False
    return True


MATURE_TESTS = [
    ('not_declining', _test_mature_not_declining),
    ('margin_quality', _test_mature_margin_quality),
    ('cash_generation', _test_mature_cash_generation),
    ('moat', _test_mature_moat),
    ('stability', _test_mature_stability),
]


# ─── GROWTH rubric (5 tests) ──────────────────────────────────────────────────
# Reward revenue pace + a clear path to profits. Hold to efficient-frontier
# unit economics (GM ≥ 50%, R40 ≥ 40).

def _test_growth_growth_rate(m):
    """Pass if YoY ≥ 12% AND (trajectory not decelerating OR YoY > 30%).

    Threshold is aligned with the classifier cut (T=12% in
    ``classify_archetype``). Phase 1.9c (2026-04-20) lowered this from 15%
    → 12% to close the 12–15% dead-band: under the prior bar, MSFT (14.9%),
    GOOGL (15.1%), AMZN (12.4%), PANW (14.9%) were classified GROWTH but
    immediately failed ``growth_rate``, landing WATCH despite strong unit
    economics. Matching the test floor to the classifier means "GROWTH by
    classifier" and "passes growth_rate" agree on the boundary case.

    The 30% decel bypass (locked 2026-04-18) is the "NVDA rule": hyper-growers
    mechanically decelerate off prior triple-digit comps (NVDA 65% off ~100%
    YoY reads as 'decelerating'). At > 30% the business is still clearly
    growth-phase; don't punish the comp effect.
    """
    yoy = m.get('revenue_yoy_growth')
    trj = m.get('growth_trajectory')
    if yoy is None:
        return None
    if yoy < 0.12:
        return False
    if trj == 'decelerating' and yoy <= 0.30:
        return False
    return True


def _test_growth_unit_economics(m):
    """Pass if gross margin ≥ 50% (software/pharma/premium-brand line)."""
    gm = m.get('gross_margin_ttm')
    if gm is None:
        return None
    return gm >= 0.50


def _test_growth_path_to_profits(m):
    """Pass if OCF > 0 OR Rule-of-40 ≥ 40 (cash-positive today OR on the
    efficient frontier as 'not-yet-profitable-but-on-track')."""
    ocf = m.get('operating_cash_flow_ttm')
    r40 = m.get('rule_40_score')
    if ocf is None and r40 is None:
        return None
    if ocf is not None and ocf > 0:
        return True
    if r40 is not None and r40 >= 40.0:
        return True
    return False


def _test_growth_moat(m):
    """Shared with MATURE — see `_test_mature_moat` docstring."""
    return _test_mature_moat(m)


def _test_growth_capital_efficiency(m):
    """Pass if Rule-of-40 ≥ 40 (tightened from Path-A's ≥ 25 — GROWTH held
    to the efficient-frontier line, not the mid-tier)."""
    r40 = m.get('rule_40_score')
    if r40 is None:
        return None
    return r40 >= 40.0


GROWTH_TESTS = [
    ('growth_rate', _test_growth_growth_rate),
    ('unit_economics', _test_growth_unit_economics),
    ('path_to_profits', _test_growth_path_to_profits),
    ('moat', _test_growth_moat),
    ('capital_efficiency', _test_growth_capital_efficiency),
]


# ─── Per-archetype dealbreakers ───────────────────────────────────────────────
# Any True → AVOID verdict, bypassing the test count.

def _compute_dealbreakers(m, archetype):
    """Archetype-specific dealbreakers.

    MATURE:
      - cagr_shrinking: 3y CAGR < −5%  (terminally declining, not slow)
      - diluting:       flag_diluting   (share-count up > 15% in 3y)

    GROWTH:
      - burning_cash:   flag_burning_cash  (FCF < 0 AND short runway)

    `flag_diluting` deliberately NOT a GROWTH dealbreaker (Phase 1.9,
    2026-04-18): SaaS stock-based-comp creep trips NOW/NFLX/PANW legitimately.
    MATURE rewards capital discipline via `stability` test + dealbreaker;
    GROWTH shuts down only on actual cash-burn risk.

    `flag_spac_or_microcap` dropped entirely: Phase 1.0 already floors at
    $1B mcap so this fires for nobody in the prescreened universe.
    """
    if archetype == 'MATURE':
        cagr = m.get('revenue_3y_cagr')
        return {
            'cagr_shrinking': (cagr is not None and cagr < -0.05),
            'diluting': bool(m.get('flag_diluting')),
        }
    if archetype == 'GROWTH':
        return {
            'burning_cash': bool(m.get('flag_burning_cash')),
        }
    return {}  # UNKNOWN → no dealbreakers (will fall to INSUFFICIENT_DATA anyway)


# ─── Scoring + verdict ────────────────────────────────────────────────────────

def score_ticker(metrics):
    """Enrich the metrics dict with archetype, tests, dealbreakers, score,
    verdict. Phase 1.9: routes through the MATURE or GROWTH rubric based
    on `classify_archetype(m)`."""
    m = dict(metrics)

    # Round 7c: derive canonical (sector, industry_group, industry) via
    # classifier.classify. fundamental_metrics now returns the raw SEC SIC
    # description under m['sic_description']; m has no 'sector' key on the
    # way in. The classifier produces the canonical 10-bucket sector and
    # the three-tier hierarchy, which we set here so downstream code (CSV
    # row, verdict_provider, frontend) reads canonical sector everywhere.
    _sector, _industry_group, _industry = classify(
        m.get('symbol'), m.get('sic'), m.get('sic_description')
    )
    m['sector'] = _sector
    m['industry_group'] = _industry_group
    m['industry'] = _industry

    archetype = classify_archetype(m)
    if archetype == 'GROWTH':
        test_list = GROWTH_TESTS
    elif archetype == 'MATURE':
        test_list = MATURE_TESTS
    else:  # UNKNOWN — no rubric applies
        test_list = []

    tests = {name: fn(m) for name, fn in test_list}
    passes = sum(1 for v in tests.values() if v is True)
    known = sum(1 for v in tests.values() if v is not None)

    dealbreakers = _compute_dealbreakers(m, archetype)
    any_dealbreaker = any(dealbreakers.values())

    # Score (0–100)
    #   5 tests × 15 pts = 75 max from tests
    #   + 10 for no dealbreakers (if we have data)
    #   + 5 each for three quality bonuses (ROIC ≥ 20%, R40 ≥ 40, SVR ≤ sector median)
    score = passes * 15
    if known > 0 and not any_dealbreaker:
        score += 10
    if (m.get('roic_ttm') or 0) >= 0.20:
        score += 5
    if (m.get('rule_40_score') or 0) >= 40.0:
        score += 5
    svr_rel = m.get('svr_vs_sector_median')
    if svr_rel is not None and svr_rel <= 1.0:
        score += 5
    score = min(score, 100)

    verdict = _verdict(m, passes, known, any_dealbreaker, archetype)

    m['tests'] = tests
    m['tests_passed'] = passes
    m['tests_known'] = known
    m['dealbreakers'] = dealbreakers
    m['any_dealbreaker'] = any_dealbreaker
    m['good_firm_score'] = score
    m['verdict'] = verdict
    m['archetype'] = archetype
    return m


def _verdict(m, passes, known, any_dealbreaker, archetype):
    """Phase 1.9 4-verdict schema. LEADER/GEM split on sector rank at 5/5."""
    # Not enough test data (ETFs, ADRs, newly listed, ingestion gaps)
    if archetype == 'UNKNOWN' or known < 3:
        return 'INSUFFICIENT_DATA'

    # Dealbreakers short-circuit
    if any_dealbreaker:
        return 'AVOID'
    if passes <= 2:
        return 'AVOID'

    rank = m.get('market_cap_rank_in_sector')

    if passes == 5:
        if rank is not None and rank <= 5:
            return 'LEADER'
        return 'GEM'
    # passes == 3 or 4 → WATCH (4/5 decel distinction collapsed into WATCH
    # since per-archetype growth tests already handle decel semantics)
    return 'WATCH'


# ─── Sector context (rank, SVR-vs-median) ────────────────────────────────────

def _sector_key(m):
    """Major SIC group = first 2 digits of SIC code."""
    sic = m.get('sic')
    if not sic:
        return None
    return sic[:2]


def apply_sector_context(all_metrics, min_peers=3):
    """
    Enrich each metrics dict with market_cap_rank_in_sector, svr_vs_sector_median,
    and sector_peers, grouped by SIC 2-digit major group.

    Sectors with fewer than `min_peers` tickers skip ranking (too noisy to be
    meaningful).
    """
    buckets = {}
    for m in all_metrics:
        key = _sector_key(m)
        if key is None:
            continue
        buckets.setdefault(key, []).append(m)

    for key, peers in buckets.items():
        sector_size = len(peers)
        if sector_size < min_peers:
            for p in peers:
                p['sector_peers'] = sector_size
            continue

        # Rank by market cap (desc)
        ranked = sorted(
            [p for p in peers if p.get('market_cap')],
            key=lambda p: p['market_cap'],
            reverse=True,
        )
        for i, p in enumerate(ranked, 1):
            p['market_cap_rank_in_sector'] = i

        # Sector SVR median (exclude missing)
        svrs = [p['svr'] for p in peers if p.get('svr') and p['svr'] > 0]
        if svrs:
            med = median(svrs)
            for p in peers:
                if p.get('svr'):
                    p['svr_vs_sector_median'] = p['svr'] / med

        for p in peers:
            p['sector_peers'] = sector_size

    return all_metrics


# ─── Full screen ──────────────────────────────────────────────────────────────

def run_full_screen(symbols=None, conn=None):
    """
    Score all tickers. Default: everything in Tickers.csv.
    Returns a list sorted by good_firm_score desc, market_cap desc.
    """
    if symbols is None:
        symbols = load_tickers_from_csv()
    if conn is None:
        conn = get_db()

    raw = [compute_metrics(s, conn=conn) for s in symbols]
    apply_sector_context(raw)
    scored = [score_ticker(m) for m in raw]

    scored.sort(key=lambda m: (
        -(m.get('good_firm_score') or 0),
        -(m.get('market_cap') or 0),
    ))
    return scored


def run_single(symbol, cached_full_screen=None, conn=None):
    """
    Score one ticker. Preferred path: reuse a prior full screen for sector
    context (cheap). Fallback: run full screen (expensive but correct).
    """
    sym = symbol.upper().strip()
    if cached_full_screen:
        for m in cached_full_screen:
            if m.get('symbol') == sym:
                return m
    scored = run_full_screen(conn=conn)
    for m in scored:
        if m.get('symbol') == sym:
            return m
    return None


# ─── CLI ──────────────────────────────────────────────────────────────────────

VERDICT_EMOJI = {
    'LEADER': '[LEADER]',
    'GEM':    '[ GEM  ]',
    'WATCH':  '[WATCH ]',
    'AVOID':  '[AVOID ]',
    'INSUFFICIENT_DATA': '[ n/a  ]',
}


def _fmt_pct(v, w=6):
    return f"{v*100:>{w-1}.1f}%" if v is not None else f"{'-':>{w}s}"


def _fmt_num(v, w=6):
    return f"{v:>{w}.1f}" if v is not None else f"{'-':>{w}s}"


def _print_table(scored):
    hdr = (
        f"{'SYM':6s} {'VERDICT':9s} {'ARCHETYPE':10s} {'SCR':>4s} {'P':>2s} "
        f"{'Rev_YoY':>7s} {'DivY':>5s} {'GM':>6s} {'ROIC':>6s} "
        f"{'SVR':>6s} {'R40':>6s} {'Trend':>12s}  {'Sector'}"
    )
    print(hdr)
    print('-' * len(hdr))
    for m in scored:
        tag = VERDICT_EMOJI.get(m['verdict'], m['verdict'])
        print(
            f"{m['symbol']:6s} "
            f"{tag:9s} "
            f"{(m.get('archetype') or '-')[:10]:10s} "
            f"{m.get('good_firm_score', 0):>4d} "
            f"{m.get('tests_passed', 0):>2d} "
            f"{_fmt_pct(m.get('revenue_yoy_growth'), 7)} "
            f"{_fmt_pct(m.get('dividend_yield'), 5)} "
            f"{_fmt_pct(m.get('gross_margin_ttm'), 6)} "
            f"{_fmt_pct(m.get('roic_ttm'), 6)} "
            f"{_fmt_num(m.get('svr'), 6)} "
            f"{_fmt_num(m.get('rule_40_score'), 6)} "
            f"{(m.get('growth_trajectory') or '-')[:12]:>12s}  "
            f"{(m.get('sector') or '-')[:42]}"
        )


# CSV export schema — kept flat and stable so leader_selector.py + any future
# downstream tool can rely on it.
CSV_OUT_FIELDS = [
    'symbol', 'name', 'sector', 'sic', 'market_cap', 'dividend_yield',
    'verdict', 'good_firm_score', 'archetype',
    'tests_passed', 'tests_known',
    'market_cap_rank_in_sector', 'sector_peers',
    # Metrics (Phase 1.9: added fcf_margin_ttm; downstream audit diagnostics
    # read it directly instead of back-computing from rule_40_score)
    'revenue_yoy_growth', 'revenue_3y_cagr', 'growth_trajectory',
    'gross_margin_ttm', 'operating_margin_ttm',
    'operating_cash_flow_ttm', 'free_cash_flow_ttm',
    'fcf_margin_ttm', 'rule_40_score',
    'roic_ttm', 'svr', 'svr_vs_sector_median',
    'flag_diluting', 'flag_burning_cash', 'flag_spac_or_microcap',
    # Bucket 2 (2026-04-21): serialized per-test + dealbreaker maps so the
    # verdict card's test-dot row + flag chips can render from CSV alone
    # (no live DB read). verdict_provider.load_verdict_for_symbol consumes
    # these. Empty strings on legacy rows -> renders as dashes (graceful
    # degradation via testDot(None) and flagChips on {}).
    'tests_json', 'dealbreakers_json',
    # Round 7c (FB-1 data half): canonical industry_group + industry from
    # classifier.classify. The existing `sector` column above (kept in name
    # and order) is now also populated with the classifier sector instead of
    # the raw SIC description — this is a data quality fix without a
    # schema-position change. Two new columns appended; no existing column
    # renamed or reordered.
    'industry_group', 'industry',
]


def write_screener_csv(scored, path):
    """Write a flat CSV of screener results suitable for leader_selector.py."""
    path = Path(path)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_OUT_FIELDS)
        writer.writeheader()
        for m in scored:
            row = {k: m.get(k, '') for k in CSV_OUT_FIELDS}
            # Serialize the per-test and dealbreaker maps as compact JSON
            # so the verdict card renders from CSV alone. Kept JSON (not
            # repeated columns) to keep schema stable as tests evolve.
            tests = m.get('tests')
            row['tests_json'] = json.dumps(tests, separators=(',', ':')) if tests else ''
            dealbreakers = m.get('dealbreakers')
            row['dealbreakers_json'] = (
                json.dumps(dealbreakers, separators=(',', ':'))
                if dealbreakers else ''
            )
            # Normalize booleans and Nones for CSV cleanliness
            for k, v in list(row.items()):
                if v is None:
                    row[k] = ''
                elif isinstance(v, bool):
                    row[k] = '1' if v else '0'
            writer.writerow(row)
    print(f"Wrote {len(scored)} rows to {path}")


def main():
    p = argparse.ArgumentParser(description="Good Firm Framework screener")
    p.add_argument("--ticker", help="Show verdict for one ticker")
    p.add_argument("--symbols", help="Comma-separated list, e.g. ISRG,SYK,ZBH")
    p.add_argument("--all", action="store_true", help="Score all tickers in Tickers.csv")
    p.add_argument("--universe", metavar="CSV",
                   help="Screen every ticker from a universe CSV "
                        "(e.g. universe_prescreened.csv from Layer 1)")
    p.add_argument("--csv-out", metavar="PATH",
                   help="Write flat CSV of results (e.g. screener_results.csv)")
    p.add_argument("--json", action="store_true", help="Output JSON instead of table")
    args = p.parse_args()

    # Resolve the universe: --symbols > --universe > --ticker (single) > --all / default
    universe = None
    if args.symbols:
        universe = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    elif args.universe:
        universe = _load_universe_symbols(args.universe)
        if not universe:
            print(f"No symbols found in {args.universe}")
            return

    if args.ticker and not args.all and not universe:
        scored = run_full_screen()
        target = next((m for m in scored if m['symbol'] == args.ticker.upper()), None)
        if not target:
            print(f"No data for {args.ticker}")
            return
        print(json.dumps(target, indent=2, default=str) if args.json
              else _print_table([target]))
        return

    scored = run_full_screen(symbols=universe)

    # If --ticker was given alongside --symbols/--universe, narrow output to that one row
    if args.ticker:
        sym = args.ticker.upper()
        scored = [m for m in scored if m.get('symbol') == sym]
        if not scored:
            print(f"No data for {args.ticker}")
            return

    if args.csv_out:
        write_screener_csv(scored, args.csv_out)

    if args.json:
        print(json.dumps(scored, indent=2, default=str))
    elif not args.csv_out:
        _print_table(scored)


if __name__ == "__main__":
    main()
