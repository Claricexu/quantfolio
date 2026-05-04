"""
fundamental_screener.py
───────────────────────
Phase 1.9 — 2-archetype rubric. Each ticker is classified as MATURE or
GROWTH on a binary Revenue-YoY split (threshold `CLASSIFIER_YOY_THRESHOLD`,
locked at 12% per `diag_threshold_sensitivity.py` 2026-04-18); per-archetype
5-test rubrics + per-archetype dealbreakers then produce a 4-verdict schema:

    LEADER  — 5/5 tests + no dealbreaker  (Round 9a: rank dimension dropped)
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


# ─── Forensic flags (Round May 15) ────────────────────────────────────────────
# A SEPARATE layer from dealbreakers. Dealbreakers gate the verdict
# (LEADER/WATCH/AVOID/INSUFFICIENT_DATA — pure quality, Round 9a invariant).
# Forensic flags ride alongside on `forensic_flags_json` + `forensic_flag_count`
# so downstream consumers (leader_selector pool filter, frontend chips) can
# act on them without touching the verdict logic.
#
# Sector exclusions: banks, insurance, REITs file under accounting frameworks
# where the flagged behaviour is normal (e.g. NI > OCF for an insurer is a
# float-investment pattern, not a forensic concern). Exclusion is done by
# SIC code range directly rather than by classifier triple — the classifier's
# `industry_group` for banks ("Capital Markets") collides with REITs and with
# V/MA payment-network overrides, and the `industry` tier is replaced with
# SEC's free-text `sic_description` on the resolution path so the fallback
# strings never appear on real rows.
#
#   - Banks/brokers/financial holding cos: SIC 6000-6299
#   - Insurance carriers / agents:         SIC 6300-6499
#   - Real Estate / Investment / REITs:    SIC 6500-6799
#
# Excludes ~all of SIC division H (Finance, Insurance, Real Estate). Payments
# overrides (V/MA, classifier.TICKER_OVERRIDES) keep their own SIC code (6199)
# so they fall in the bank-exclusion bucket too — which is fine, payment
# networks have float-vs-cash dynamics that distort NI/OCF the same way banks
# do. Trade-off accepted; revisit if a payments-specific carve-out becomes
# necessary.

# Override file: see _load_forensic_flag_overrides (commit 3 of this round).
# Wired into the count below.


_FORENSIC_OVERRIDES_PATH = Path(__file__).resolve().parent / 'cache' / 'forensic_flag_overrides.csv'

# Module-level cache. Loaded once on first access via `_get_forensic_overrides()`
# and reused for the rest of the process. Quarterly rebuild reads it once at
# the start; if the CSV is edited mid-rebuild the change is not picked up
# until the next process — that's the documented contract (race-condition
# avoidance per directive). Tests can reset this with `reset_overrides_cache()`.
_FORENSIC_OVERRIDES_CACHE = None


def _load_forensic_flag_overrides(path=None):
    """Read cache/forensic_flag_overrides.csv into a dict.

    Returns ``{(symbol_upper, flag_name): expires_at_date}``. Missing file
    or empty file → empty dict (override path becomes a no-op, identical
    to the commit-2 stub behaviour).

    The file is read ONCE per process. If it's edited mid-rebuild, the
    change is not picked up until the next screener invocation — this is
    by design to keep the loader race-free during quarterly rebuilds.
    Hot-reload would require either file-locking or a per-row mtime check,
    neither of which is justified by current ops cadence (quarterly).

    Lines starting with '#' are treated as comments so the schema example
    can sit inline in the CSV without polluting the override map.

    Malformed rows (bad date, missing column, unknown flag) are SKIPPED
    with a stderr warning rather than raising — a typo in this file should
    not break the screener for the entire universe.
    """
    from datetime import date
    import csv as _csv
    import sys as _sys

    path = Path(path) if path else _FORENSIC_OVERRIDES_PATH
    if not path.exists():
        return {}

    valid_flags = {name for name, _fn in FORENSIC_FLAGS}
    out = {}
    try:
        with path.open('r', newline='', encoding='utf-8') as f:
            # Strip comment lines BEFORE handing to DictReader so '#' rows
            # don't confuse the column parser.
            lines = [ln for ln in f.read().splitlines()
                     if ln.strip() and not ln.lstrip().startswith('#')]
            if not lines:
                return {}
            reader = _csv.DictReader(lines)
            for row in reader:
                sym = (row.get('symbol') or '').strip().upper()
                flag = (row.get('flag_name') or '').strip()
                exp_raw = (row.get('expires_at') or '').strip()
                if not sym or not flag or not exp_raw:
                    continue
                if flag not in valid_flags:
                    print(f"  [warn] forensic override: unknown flag "
                          f"'{flag}' for {sym} — skipping",
                          file=_sys.stderr)
                    continue
                try:
                    exp_date = date.fromisoformat(exp_raw)
                except ValueError:
                    print(f"  [warn] forensic override: bad expires_at "
                          f"'{exp_raw}' for {sym}/{flag} — skipping",
                          file=_sys.stderr)
                    continue
                out[(sym, flag)] = exp_date
    except Exception as e:
        print(f"  [warn] forensic override file unreadable ({path}): {e} "
              f"— proceeding with no overrides", file=_sys.stderr)
        return {}
    return out


def _get_forensic_overrides():
    """Return the overrides dict for the current screener run.

    Lazy-loaded once and cached at module scope. Test code can call
    ``reset_forensic_overrides_cache()`` to force re-read between scenarios.
    """
    global _FORENSIC_OVERRIDES_CACHE
    if _FORENSIC_OVERRIDES_CACHE is None:
        _FORENSIC_OVERRIDES_CACHE = _load_forensic_flag_overrides()
    return _FORENSIC_OVERRIDES_CACHE


def reset_forensic_overrides_cache():
    """Clear the module-level overrides cache. Used by tests to swap in a
    different override file between scenarios; production code does not
    need to call this (the cache is right for the duration of one run)."""
    global _FORENSIC_OVERRIDES_CACHE
    _FORENSIC_OVERRIDES_CACHE = None


def _is_forensic_excluded_sector(m):
    """True if this row's SIC code falls in the Finance/Insurance/Real Estate
    division (SIC 6000-6799). Banks, insurers, and REITs file under accounting
    frameworks where the flagged behaviours are normal."""
    sic = m.get('sic')
    if sic is None or sic == '':
        return False
    try:
        sic_int = int(float(sic))
    except (TypeError, ValueError):
        return False
    return 6000 <= sic_int <= 6799


def _flag_ni_ocf_divergence(m):
    """Flag if GAAP NetIncome > OperatingCashFlow for 3 consecutive FYs.

    Returns False (not None) when histories are short — we don't fail-open
    here because a partial history doesn't mean the divergence happened;
    if the data isn't there we can't claim the flag, which is the same
    semantically as "didn't trip."
    """
    ni = m.get('ni_3y_history') or []
    ocf = m.get('ocf_3y_history') or []
    if len(ni) < 3 or len(ocf) < 3:
        return False
    # Both lists are newest-first; require strict NI > OCF in every one
    # of the last 3 fiscal years.
    return all(
        (n is not None and o is not None and n > o)
        for n, o in zip(ni[:3], ocf[:3])
    )


def _flag_leverage_high(m):
    """Flag if Net Debt / EBITDA > 4x AND Interest Coverage < 2x.

    Combined AND threshold — both legs must trip. Net Debt > 0 and EBITDA
    > 0 both required for the ratio to be meaningful (a cash-rich firm with
    negative net debt should never flag, regardless of EBITDA shape).
    """
    nd = m.get('net_debt')
    ebitda = m.get('ebitda_ttm')
    icov = m.get('interest_coverage_ratio')
    if nd is None or ebitda is None or icov is None:
        return False
    if ebitda <= 0:
        # Negative or zero EBITDA — leverage is undefined here; the
        # underlying problem already shows up in burning_cash + the
        # rubric tests, no need to double-flag via this leg.
        return False
    if nd <= 0:
        return False
    nd_ebitda = nd / ebitda
    return nd_ebitda > 4.0 and icov < 2.0


def _flag_going_concern(m):
    """Flag if SubstantialDoubtAboutGoingConcern XBRL fact is True.

    NOT IMPLEMENTED — the canonical us-gaap tag is not exposed by SEC's
    companyfacts/frames API. See fundamental_metrics.compute_metrics
    `going_concern_present` stub for the full investigation. Kept as a
    schema-stable False so the column count is consistent for the day
    a 10-K-narrative-parsing pipeline lands.
    """
    return bool(m.get('going_concern_present'))


def _flag_dilution_velocity(m):
    """Flag if shares-outstanding YoY growth > 10% in the most recent year.

    Complements the existing `flag_diluting` (15% over 3y), which can miss
    a single-year burst that resets to baseline. Single-year > 10% is a
    sharper detector for one-off raises / large equity issuances.
    """
    g = m.get('shares_outstanding_yoy_growth')
    if g is None:
        return False
    return g > 0.10


# Stable iteration order — frontend chips render in this order, count is
# computed from the same map.
FORENSIC_FLAGS = (
    ('ni_ocf_divergence', _flag_ni_ocf_divergence),
    ('leverage_high', _flag_leverage_high),
    ('going_concern', _flag_going_concern),
    ('dilution_velocity', _flag_dilution_velocity),
)


def _compute_forensic_flags(m, overrides=None):
    """Round May 15 forensic flags. Mirrors `_compute_dealbreakers` in shape
    but lives in its own column family — does NOT modify the Round 9a
    verdict (LEADER/WATCH/AVOID/INSUFFICIENT_DATA stays pure quality).

    Returns ``(flags_dict, count)`` where:
      - ``flags_dict`` is the FULL raw map of every flag (including any
        overridden-but-True flags) so the UI can show "suppressed" state.
      - ``count`` is the number of flags that are True AND not currently
        overridden — single source of truth per PATTERNS.md P-4.

    Sector-excluded rows return ({}, 0) — banks/insurance/REITs file under
    accounting frameworks where these behaviours are normal.

    `overrides` is the dict produced by `_load_forensic_flag_overrides`,
    ``{(symbol, flag_name): expires_at_date}``. None or empty dict skips
    the override lookup entirely (so unit tests can call this without
    touching the override CSV).
    """
    if _is_forensic_excluded_sector(m):
        return {}, 0
    raw = {name: bool(fn(m)) for name, fn in FORENSIC_FLAGS}

    # Override: a flag is "suppressed" if (symbol, flag_name) is in the
    # override map AND today's date < expires_at. Suppressed flags stay
    # True in the raw map (so the UI can show them as overridden) but
    # don't contribute to the count.
    if overrides:
        from datetime import date
        sym = (m.get('symbol') or '').upper()
        today = date.today()
        suppressed = {
            name for (s, name), exp in overrides.items()
            if s == sym and today < exp
        }
    else:
        suppressed = set()

    count = sum(1 for name, v in raw.items() if v and name not in suppressed)
    return raw, count


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

    # Forensic flags — separate layer from dealbreakers, computed once here
    # so leader_selector + leaders.csv + frontend rendering all read from
    # the same column (PATTERNS.md P-4 single-source-of-truth).
    forensic_flags, forensic_count = _compute_forensic_flags(
        m, overrides=_get_forensic_overrides()
    )

    # Score (0–100)
    #   5 tests × 15 pts = 75 max from tests
    #   + 10 for no dealbreakers (if we have data)
    #   + 5 each for two quality bonuses (ROIC ≥ 20%, R40 ≥ 40)
    # Round 7d: dropped the SVR-vs-sector-median bonus alongside removal of
    # `svr_vs_sector_median` from the CSV. Peer-median benchmarking now lives
    # in the per-metric `peer_median_*` columns (industry_group buckets,
    # min_peers=5) rather than as a scoring lever. Score drift on existing
    # rows is bounded to -5 (rows previously hitting the bonus); see CHANGELOG.
    # Theoretical max: 5*15 + 10 + 5 + 5 = 95. The earlier `min(score, 100)`
    # cap was dead code (Round 9a removal).
    score = passes * 15
    if known > 0 and not any_dealbreaker:
        score += 10
    if (m.get('roic_ttm') or 0) >= 0.20:
        score += 5
    if (m.get('rule_40_score') or 0) >= 40.0:
        score += 5

    verdict = _verdict(m, passes, known, any_dealbreaker, archetype)

    m['tests'] = tests
    m['tests_passed'] = passes
    m['tests_known'] = known
    m['dealbreakers'] = dealbreakers
    m['any_dealbreaker'] = any_dealbreaker
    m['good_firm_score'] = score
    m['verdict'] = verdict
    m['archetype'] = archetype
    # Forensic flags — separate layer; verdict above is unchanged.
    m['forensic_flags'] = forensic_flags
    m['forensic_flag_count'] = forensic_count
    return m


def _verdict(m, passes, known, any_dealbreaker, archetype):
    """Round 9a 4-verdict schema (size-blind).

    Round 9a (2026-05-03): collapsed the LEADER/GEM split. The pre-9a schema
    used `market_cap_rank_in_sector ≤ 5` to differentiate top-of-sector
    LEADERs from smaller-cap GEMs at 5/5 quality; that turned size into a
    quality label even though the rubric tests were identical. Verdicts now
    encode pure quality. Sector-rank context still rides on the row via
    `market_cap_rank_in_sector`, just not at the verdict layer.
    """
    # Not enough test data (ETFs, ADRs, newly listed, ingestion gaps)
    if archetype == 'UNKNOWN' or known < 3:
        return 'INSUFFICIENT_DATA'

    # Dealbreakers short-circuit
    if any_dealbreaker:
        return 'AVOID'
    if passes <= 2:
        return 'AVOID'

    if passes == 5:
        return 'LEADER'
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
    Enrich each metrics dict with market_cap_rank_in_sector and sector_peers,
    grouped by SIC 2-digit major group.

    Sectors with fewer than `min_peers` tickers skip ranking (too noisy to be
    meaningful).

    Round 7d: removed the SVR-vs-sector-median computation. The peer-median
    benchmarking surface now lives in `apply_peer_medians` (called separately,
    bucketed by `industry_group` not SIC-2, min_peers=5) which writes the
    `peer_median_*` family of columns. The market-cap rank logic stays here
    because it's still SIC-2 keyed and surfaced on the row for context (and
    consumed by the moat-fallback test); Round 9a dropped its role in the
    verdict, but the column itself stays.
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

        for p in peers:
            p['sector_peers'] = sector_size

    return all_metrics


# ─── Peer-median benchmarking (industry_group bucket, min_peers=5) ───────────

# The 8 numeric metrics surfaced on the verdict card that have a meaningful
# peer comparison. Categorical fields (Growth Trajectory) and absolute-magnitude
# fields (Revenue TTM, OCF, FCF, Market Cap) are excluded by design.
PEER_MEDIAN_METRICS = (
    'revenue_yoy_growth',
    'revenue_3y_cagr',
    'gross_margin_ttm',
    'operating_margin_ttm',
    'fcf_margin_ttm',
    'rule_40_score',
    'roic_ttm',
    'svr',
)


def apply_peer_medians(scored, min_peers=5):
    """
    Bucket rows by `industry_group` and compute peer medians for the 8 metrics
    in `PEER_MEDIAN_METRICS`. Writes `peer_median_{metric}` back to each row
    dict (None if fewer than `min_peers` non-null values for that metric in the
    bucket). Also writes `peer_count` = the size of that row's industry_group
    bucket (regardless of metric availability) so the verdict card can render
    "n=12" tooltips later.

    Rows with no `industry_group` (ETFs, classifier gaps) get neither
    `peer_median_*` nor `peer_count` written — they remain absent so the CSV
    serialization emits empty strings.

    Round 7d: bucketing is by industry_group (29 fine-grained groups, all
    ≥5 members per classifier.py:11) rather than SIC-2, so peer comparisons
    are tight (Semiconductors vs all of "37" Transportation Equipment).
    """
    buckets = {}
    for m in scored:
        ig = m.get('industry_group')
        if not ig:
            continue
        buckets.setdefault(ig, []).append(m)

    for ig, peers in buckets.items():
        bucket_size = len(peers)
        # peer_count is the bucket size — independent of per-metric coverage.
        for p in peers:
            p['peer_count'] = bucket_size

        for metric in PEER_MEDIAN_METRICS:
            values = [p[metric] for p in peers
                      if p.get(metric) is not None]
            if len(values) < min_peers:
                med = None
            else:
                med = median(values)
            col = f'peer_median_{metric}'
            for p in peers:
                p[col] = med

    return scored


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
    # Round 7d: peer medians run AFTER scoring (industry_group is set during
    # compute_metrics via classifier.classify, so it's available on `raw`
    # already, but we run on `scored` to keep the writeback site obvious to
    # future readers — every column the CSV emits is touched in this function).
    apply_peer_medians(scored)

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
    'roic_ttm', 'svr', 'pe_trailing',
    'flag_diluting', 'flag_burning_cash', 'flag_spac_or_microcap',
    # Bucket 2 (2026-04-21): serialized per-test + dealbreaker maps so the
    # verdict card's test-dot row + flag chips can render from CSV alone
    # (no live DB read). verdict_provider.load_verdict_for_symbol consumes
    # these. Empty strings on legacy rows -> renders as dashes (graceful
    # degradation via testDot(None) and flagChips on {}).
    'tests_json', 'dealbreakers_json',
    # Round May 15: forensic flags. SEPARATE layer from dealbreakers — does
    # NOT modify the verdict (Round 9a invariant: verdicts encode pure
    # quality). `forensic_flags_json` is the full raw map (suppressed flags
    # included so the UI can render "overridden" state); `forensic_flag_count`
    # is the count AFTER override application — single source of truth per
    # PATTERNS.md P-4. leader_selector reads forensic_flag_count for pool
    # eligibility (must == 0).
    'forensic_flags_json', 'forensic_flag_count',
    # Round 7c (FB-1 data half): canonical industry_group + industry from
    # classifier.classify. The existing `sector` column above (kept in name
    # and order) is now also populated with the classifier sector instead of
    # the raw SIC description — this is a data quality fix without a
    # schema-position change. Two new columns appended; no existing column
    # renamed or reordered.
    'industry_group', 'industry',
    # Round 7d: 8 peer-median columns + bucket size, computed by
    # `apply_peer_medians` (industry_group bucket, min_peers=5). Empty on
    # rows with no industry_group (ETFs, classifier gaps) or whose bucket
    # has <5 non-null values for the metric. `peer_count` = total bucket
    # size; reserved for future "n=12" tooltip on the verdict card.
    # `svr_vs_sector_median` was removed in this round (the SIC-2 ratio is
    # superseded by the industry_group peer median for SVR specifically).
    'peer_median_revenue_yoy_growth',
    'peer_median_revenue_3y_cagr',
    'peer_median_gross_margin_ttm',
    'peer_median_operating_margin_ttm',
    'peer_median_fcf_margin_ttm',
    'peer_median_rule_40_score',
    'peer_median_roic_ttm',
    'peer_median_svr',
    'peer_count',
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
            # Round May 15: forensic flags carry the full raw map (override-
            # suppressed flags stay True in this map for UI transparency)
            # and the count is computed after override application.
            forensic = m.get('forensic_flags')
            row['forensic_flags_json'] = (
                json.dumps(forensic, separators=(',', ':'))
                if forensic else ''
            )
            # forensic_flag_count is already a plain int; the bool/None
            # normalization below would coerce 0 → '0' which is fine.
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
