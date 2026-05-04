"""Unit tests for ``leader_selector.select_leaders`` — Round May 15.

Plain-assert style, matching the rest of ``tests/unit/``. The function
under test is pure over a list of dicts (no I/O), which makes the
forensic-flag pool filter cheap to exercise from a synthetic CSV-shape
input.

What's pinned here:

  - Round 9a (already shipped): only LEADER rows are eligible; rows
    flagged with a dealbreaker still get filtered defensively.
  - Round May 15 (this round): rows with ``forensic_flag_count > 0`` are
    excluded from the LEADER pool. ``leader_selector._has_active_forensic_flags``
    reads the post-override count (single source of truth, PATTERNS.md
    P-4) so the selector never needs to re-apply overrides.
  - Round May 15: legacy rows without the column (blank cell) pass
    through as if forensic_flag_count == 0 — graceful degradation for
    pre-Round-May-15 ``screener_results.csv`` files.
  - Round May 15: under-fill log line indicates how many were dropped
    by the forensic-flag screen so the operator can tell forensic-driven
    pool shrinkage apart from genuinely-too-few-LEADERs.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from leader_selector import (  # noqa: E402
    _has_active_forensic_flags,
    select_leaders,
)


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    except Exception as exc:
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


def _row(symbol, score, *, verdict='LEADER', forensic_flag_count='0',
         flag_diluting='0', flag_burning_cash='0',
         flag_spac_or_microcap='0', market_cap=1e9, archetype='GROWTH'):
    """Build a CSV-shape row dict (string-typed numerics, matching how
    DictReader presents rows). The selector is called with raw CSV rows
    in production — keep the shape the same here so an off-by-one between
    int-typed fixtures and string-typed reality can't sneak through."""
    return {
        'symbol': symbol,
        'verdict': verdict,
        'good_firm_score': str(score),
        'market_cap': str(market_cap),
        'archetype': archetype,
        'forensic_flag_count': str(forensic_flag_count),
        'flag_diluting': str(flag_diluting),
        'flag_burning_cash': str(flag_burning_cash),
        'flag_spac_or_microcap': str(flag_spac_or_microcap),
    }


# ─── _has_active_forensic_flags helper ────────────────────────────────────


def test_has_forensic_zero_count_returns_false():
    """forensic_flag_count == 0 → eligible (False = not flagged)."""
    assert _has_active_forensic_flags(
        {'forensic_flag_count': '0'}) is False


def test_has_forensic_positive_count_returns_true():
    """Any positive count → True (excluded)."""
    assert _has_active_forensic_flags(
        {'forensic_flag_count': '1'}) is True
    assert _has_active_forensic_flags(
        {'forensic_flag_count': '3'}) is True


def test_has_forensic_blank_cell_passes_through():
    """Legacy row (no forensic column at all, or blank cell) → False.

    Round May 15 graceful degradation: rows from a pre-round
    screener_results.csv must still flow through select_leaders without
    being dropped wholesale. The only behaviour change is that NEW rows
    with active flags get filtered — old rows are treated as 0.
    """
    assert _has_active_forensic_flags({}) is False
    assert _has_active_forensic_flags({'forensic_flag_count': ''}) is False
    assert _has_active_forensic_flags({'forensic_flag_count': None}) is False


def test_has_forensic_garbage_cell_returns_false():
    """Non-numeric cell (data corruption) → False rather than raising.

    Defense-in-depth: a one-off CSV write bug shouldn't crash the
    selector for the entire universe.
    """
    assert _has_active_forensic_flags(
        {'forensic_flag_count': 'banana'}) is False


# ─── select_leaders pool-filter integration ──────────────────────────────


def test_clean_leader_passes_pool_filter():
    """LEADER row, forensic_flag_count == 0 → included in selected pool."""
    rows = [_row('AAA', 90, forensic_flag_count='0')]
    selected, reasons = select_leaders(rows, target_size=10)
    assert len(selected) == 1
    assert reasons.get('AAA') == 'leader'


def test_flagged_leader_excluded_from_pool():
    """LEADER row, forensic_flag_count > 0 → excluded from selected pool."""
    rows = [
        _row('AAA', 95, forensic_flag_count='1'),
        _row('BBB', 80, forensic_flag_count='0'),
    ]
    selected, reasons = select_leaders(rows, target_size=10)
    selected_syms = {(r.get('symbol') or '').upper() for r in selected}
    assert 'AAA' not in selected_syms, (
        f"AAA had forensic_flag_count=1, should be excluded; got {selected_syms}")
    assert 'BBB' in selected_syms
    assert reasons.get('BBB') == 'leader'


def test_legacy_blank_forensic_count_passes_through():
    """Mixed pool: a legacy LEADER row (no forensic_flag_count cell)
    must NOT be excluded — graceful degradation for pre-Round-May-15
    screener_results.csv files."""
    legacy = _row('LEGACY', 90)
    del legacy['forensic_flag_count']  # simulate the column not existing
    rows = [legacy, _row('NEW', 85, forensic_flag_count='0')]
    selected, reasons = select_leaders(rows, target_size=10)
    selected_syms = {(r.get('symbol') or '').upper() for r in selected}
    assert 'LEGACY' in selected_syms, (
        f"legacy row missing forensic column should pass through; "
        f"got {selected_syms}")
    assert 'NEW' in selected_syms


def test_forensic_dropped_list_surfaces_for_underfill_log():
    """The selector stashes the list of forensic-dropped symbols on
    select_leaders._last_forensic_dropped so main() can surface a
    per-row [note] line distinguishing forensic-driven shrinkage from
    a genuinely-too-few-LEADERs scenario."""
    rows = [
        _row('AAA', 95, forensic_flag_count='2'),
        _row('BBB', 90, forensic_flag_count='1'),
        _row('CCC', 80, forensic_flag_count='0'),
    ]
    selected, reasons = select_leaders(rows, target_size=10)
    dropped = getattr(select_leaders, '_last_forensic_dropped', None)
    assert dropped is not None, (
        "select_leaders should expose _last_forensic_dropped for the "
        "under-fill log line")
    assert sorted(dropped) == ['AAA', 'BBB'], (
        f"expected ['AAA','BBB'] dropped; got {sorted(dropped) if dropped else dropped}")
    assert len(selected) == 1
    assert reasons.get('CCC') == 'leader'


def test_dealbreaker_still_excludes_independently_of_forensic():
    """Dealbreaker filter (defense-in-depth) still applies even when
    forensic_flag_count is 0 — the two filters are independent."""
    rows = [
        _row('AAA', 90, flag_diluting='1', forensic_flag_count='0'),
        _row('BBB', 85, forensic_flag_count='0'),
    ]
    selected, reasons = select_leaders(rows, target_size=10)
    selected_syms = {(r.get('symbol') or '').upper() for r in selected}
    assert 'AAA' not in selected_syms, (
        f"diluting LEADER should be excluded by defensive dealbreaker "
        f"check even with clean forensic flags; got {selected_syms}")
    assert 'BBB' in selected_syms


def test_non_leader_rows_skipped_regardless_of_forensic():
    """WATCH/AVOID rows are never eligible regardless of forensic count.

    Round 9a invariant: only LEADER feeds leaders.csv. Round May 15
    didn't relax that — forensic flags are a tightening on top.
    """
    rows = [
        _row('WATCHME', 75, verdict='WATCH', forensic_flag_count='0'),
        _row('AVOIDME', 30, verdict='AVOID', forensic_flag_count='0'),
        _row('LEADER1', 90, verdict='LEADER', forensic_flag_count='0'),
    ]
    selected, reasons = select_leaders(rows, target_size=10)
    selected_syms = {(r.get('symbol') or '').upper() for r in selected}
    assert selected_syms == {'LEADER1'}


# ─── Runner ────────────────────────────────────────────────────────────────


def run_all():
    fails = 0
    for name, fn in (
        ("test_has_forensic_zero_count_returns_false",
         test_has_forensic_zero_count_returns_false),
        ("test_has_forensic_positive_count_returns_true",
         test_has_forensic_positive_count_returns_true),
        ("test_has_forensic_blank_cell_passes_through",
         test_has_forensic_blank_cell_passes_through),
        ("test_has_forensic_garbage_cell_returns_false",
         test_has_forensic_garbage_cell_returns_false),
        ("test_clean_leader_passes_pool_filter",
         test_clean_leader_passes_pool_filter),
        ("test_flagged_leader_excluded_from_pool",
         test_flagged_leader_excluded_from_pool),
        ("test_legacy_blank_forensic_count_passes_through",
         test_legacy_blank_forensic_count_passes_through),
        ("test_forensic_dropped_list_surfaces_for_underfill_log",
         test_forensic_dropped_list_surfaces_for_underfill_log),
        ("test_dealbreaker_still_excludes_independently_of_forensic",
         test_dealbreaker_still_excludes_independently_of_forensic),
        ("test_non_leader_rows_skipped_regardless_of_forensic",
         test_non_leader_rows_skipped_regardless_of_forensic),
    ):
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
