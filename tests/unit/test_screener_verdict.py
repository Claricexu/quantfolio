"""Unit tests for ``fundamental_screener._verdict`` and the score formula.

Round 9a (2026-05-03) — the verdict schema collapsed from five tiers to
four (GEM removed; LEADER no longer encodes sector-rank). These tests
pin the new size-blind contract:

  - LEADER  = 5/5 tests + no dealbreaker, regardless of rank
  - WATCH   = 3-4/5 + no dealbreaker
  - AVOID   = <=2/5 OR any dealbreaker
  - INSUFFICIENT_DATA = archetype=UNKNOWN OR known<3

They also pin the score formula at its real ceiling (95) — Round 9a
removed the dead ``min(score, 100)`` cap that was never reachable.

Plain-assert style, matching the rest of ``tests/unit/``. The function
under test is pure over a single metrics dict; no DB/network/fixture
dependency.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fundamental_screener import _verdict, score_ticker  # noqa: E402


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


# ─── _verdict() direct tests ───────────────────────────────────────────────

def test_leader_5_of_5_top_rank():
    """5/5 + clean + top-5 rank → LEADER (was LEADER pre-9a too)."""
    m = {'market_cap_rank_in_sector': 1}
    v = _verdict(m, passes=5, known=5, any_dealbreaker=False,
                 archetype='GROWTH')
    assert v == 'LEADER', f"expected LEADER, got {v}"


def test_leader_5_of_5_outside_top_rank():
    """5/5 + clean + rank > 5 → LEADER (was GEM pre-9a; Round 9a unifies)."""
    m = {'market_cap_rank_in_sector': 27}
    v = _verdict(m, passes=5, known=5, any_dealbreaker=False,
                 archetype='MATURE')
    assert v == 'LEADER', f"expected LEADER, got {v}"


def test_leader_5_of_5_unknown_rank():
    """5/5 + clean + rank=None → LEADER (was GEM pre-9a)."""
    m = {}  # no market_cap_rank_in_sector at all
    v = _verdict(m, passes=5, known=5, any_dealbreaker=False,
                 archetype='GROWTH')
    assert v == 'LEADER', f"expected LEADER, got {v}"


def test_watch_three_passes():
    m = {'market_cap_rank_in_sector': 1}
    v = _verdict(m, passes=3, known=5, any_dealbreaker=False,
                 archetype='GROWTH')
    assert v == 'WATCH', f"expected WATCH, got {v}"


def test_watch_four_passes():
    m = {}
    v = _verdict(m, passes=4, known=5, any_dealbreaker=False,
                 archetype='MATURE')
    assert v == 'WATCH', f"expected WATCH, got {v}"


def test_avoid_two_passes():
    m = {}
    v = _verdict(m, passes=2, known=5, any_dealbreaker=False,
                 archetype='GROWTH')
    assert v == 'AVOID', f"expected AVOID, got {v}"


def test_avoid_zero_passes():
    m = {}
    v = _verdict(m, passes=0, known=5, any_dealbreaker=False,
                 archetype='MATURE')
    assert v == 'AVOID', f"expected AVOID, got {v}"


def test_avoid_dealbreaker_overrides_full_passes():
    """5/5 + dealbreaker → AVOID. Dealbreakers short-circuit the test count."""
    m = {'market_cap_rank_in_sector': 1}
    v = _verdict(m, passes=5, known=5, any_dealbreaker=True,
                 archetype='GROWTH')
    assert v == 'AVOID', f"expected AVOID, got {v}"


def test_insufficient_data_unknown_archetype():
    m = {}
    v = _verdict(m, passes=0, known=0, any_dealbreaker=False,
                 archetype='UNKNOWN')
    assert v == 'INSUFFICIENT_DATA', f"expected INSUFFICIENT_DATA, got {v}"


def test_insufficient_data_low_known_count():
    """known<3 → INSUFFICIENT_DATA, even on a known archetype."""
    m = {}
    v = _verdict(m, passes=2, known=2, any_dealbreaker=False,
                 archetype='GROWTH')
    assert v == 'INSUFFICIENT_DATA', f"expected INSUFFICIENT_DATA, got {v}"


def test_rank_irrelevant_at_5_of_5():
    """Round 9a contract: identical metrics with different ranks both → LEADER.

    Pre-9a the rank-3 row was LEADER and the rank-50 row was GEM. The
    Round 9a collapse is the whole point of this test — it documents the
    behaviour change in code so a future re-introduction of the rank gate
    fails loudly.
    """
    base = {'archetype_unused_marker': True}
    v_top    = _verdict({**base, 'market_cap_rank_in_sector': 3},
                        passes=5, known=5, any_dealbreaker=False,
                        archetype='MATURE')
    v_middle = _verdict({**base, 'market_cap_rank_in_sector': 50},
                        passes=5, known=5, any_dealbreaker=False,
                        archetype='MATURE')
    assert v_top == v_middle == 'LEADER', \
        f"expected both LEADER, got top={v_top} middle={v_middle}"


# ─── score_ticker() — score ceiling test ───────────────────────────────────

def test_score_max_is_95_no_artificial_cap():
    """The theoretical score max is 5*15 + 10 + 5 + 5 = 95.

    Pre-9a the code had a ``min(score, 100)`` cap which was dead. We pin
    the real ceiling at 95 so re-introducing a 100 cap (or any other
    cap below 95) shows up as a failing test.
    """
    # Hand-craft a metrics dict that hits every bonus and every test for
    # the GROWTH rubric. Values are chosen well above each threshold so
    # an off-by-one in a future rebalance still passes this test.
    m = {
        'symbol': 'TEST',
        'sic': '7372',
        'sic_description': 'Software',
        'revenue_yoy_growth': 0.45,        # GROWTH classifier (>=12%) + growth_rate test
        'revenue_3y_cagr': 0.40,
        'gross_margin_ttm': 0.80,          # unit_economics (>=50%)
        'operating_margin_ttm': 0.30,
        'fcf_margin_ttm': 0.25,
        'operating_cash_flow_ttm': 1e9,    # path_to_profits (OCF>0)
        'roic_ttm': 0.35,                  # moat (>=10%) + ROIC bonus (>=20%)
        'rule_40_score': 60.0,             # capital_efficiency + R40 bonus (>=40)
        'market_cap_rank_in_sector': 2,    # moat fallback + (legacy) leader rank
        'growth_trajectory': 'accelerating',
        'flag_diluting': False,
        'flag_burning_cash': False,
    }
    out = score_ticker(m)
    assert out['good_firm_score'] == 95, \
        f"expected 95, got {out['good_firm_score']}"
    assert out['verdict'] == 'LEADER', \
        f"expected LEADER, got {out['verdict']}"
    assert out['tests_passed'] == 5, \
        f"expected 5/5, got {out['tests_passed']}/5"


def test_score_min_is_0():
    """All-fail row with no data still scores 0 (no negatives)."""
    m = {
        'symbol': 'NULL',
        'sic': '7372',
        'sic_description': 'Software',
        'revenue_yoy_growth': None,
    }
    out = score_ticker(m)
    assert out['good_firm_score'] >= 0, \
        f"score should never be negative; got {out['good_firm_score']}"


# ─── cagr_shrinking dealbreaker: data plumbing for frontend label ──────────

def test_mature_cagr_shrinking_emits_dealbreaker_key():
    """When a MATURE row has revenue_3y_cagr < -5%, ``score_ticker`` must
    emit ``cagr_shrinking`` as a True key in the dealbreakers dict.

    This is the data the frontend's dealbreaker label map (now including
    cagr_shrinking → "Shrinking Revenue", Round 9a) reads. If the key is
    ever renamed or dropped, the frontend would silently fall back to
    rendering the literal field name — this test catches that.
    """
    m = {
        'symbol': 'CYC',
        'sic': '2911',
        'sic_description': 'Petroleum Refining',
        'revenue_yoy_growth': 0.02,       # MATURE classifier
        'revenue_3y_cagr': -0.10,         # trips cagr_shrinking (< -5%)
        'gross_margin_ttm': 0.30,
        'operating_margin_ttm': 0.12,
        'fcf_margin_ttm': 0.10,
        'operating_cash_flow_ttm': 5e9,
        'roic_ttm': 0.12,
        'rule_40_score': 14.0,
        'market_cap_rank_in_sector': 3,
        'growth_trajectory': 'stable',
        'flag_diluting': False,
        'flag_burning_cash': False,
    }
    out = score_ticker(m)
    assert out['archetype'] == 'MATURE', \
        f"expected MATURE archetype, got {out['archetype']}"
    db = out['dealbreakers']
    assert 'cagr_shrinking' in db, \
        f"dealbreakers dict missing 'cagr_shrinking' key: {db}"
    assert db['cagr_shrinking'] is True, \
        f"cagr_shrinking should be True for cagr=-0.10: {db}"
    assert out['verdict'] == 'AVOID', \
        f"cagr_shrinking is a dealbreaker → AVOID; got {out['verdict']}"


# ─── Runner ────────────────────────────────────────────────────────────────


def run_all():
    fails = 0
    for name, fn in (
        ("test_leader_5_of_5_top_rank", test_leader_5_of_5_top_rank),
        ("test_leader_5_of_5_outside_top_rank",
         test_leader_5_of_5_outside_top_rank),
        ("test_leader_5_of_5_unknown_rank", test_leader_5_of_5_unknown_rank),
        ("test_watch_three_passes", test_watch_three_passes),
        ("test_watch_four_passes", test_watch_four_passes),
        ("test_avoid_two_passes", test_avoid_two_passes),
        ("test_avoid_zero_passes", test_avoid_zero_passes),
        ("test_avoid_dealbreaker_overrides_full_passes",
         test_avoid_dealbreaker_overrides_full_passes),
        ("test_insufficient_data_unknown_archetype",
         test_insufficient_data_unknown_archetype),
        ("test_insufficient_data_low_known_count",
         test_insufficient_data_low_known_count),
        ("test_rank_irrelevant_at_5_of_5", test_rank_irrelevant_at_5_of_5),
        ("test_score_max_is_95_no_artificial_cap",
         test_score_max_is_95_no_artificial_cap),
        ("test_score_min_is_0", test_score_min_is_0),
        ("test_mature_cagr_shrinking_emits_dealbreaker_key",
         test_mature_cagr_shrinking_emits_dealbreaker_key),
    ):
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
