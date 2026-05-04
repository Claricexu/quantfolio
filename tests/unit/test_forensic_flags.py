"""Unit tests for the Round May 15 forensic-flag layer.

Plain-assert style, matching the rest of ``tests/unit/``. The functions
under test are pure over a metrics dict (no DB / network), so we hand-
craft minimal rows. Override-loader tests go through a tmp CSV via
``_load_forensic_flag_overrides(path=...)`` and reset the module-level
cache between scenarios to avoid leaking state into other tests.

The four flags under test (Round May 15):

  - ``ni_ocf_divergence``  — NI > OCF for 3 consecutive FY
  - ``leverage_high``      — Net Debt / EBITDA > 4x AND interest cov < 2x
  - ``going_concern``      — STUB (always False; see commit-7 docs for why)
  - ``dilution_velocity``  — YoY share growth > 10%

Sector exclusion: SIC 6000-6799 (banks/insurance/REITs) — for these rows
the flagged behaviour is normal, so all four flags are suppressed.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from datetime import date, timedelta

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fundamental_screener import (  # noqa: E402
    FORENSIC_FLAGS,
    _compute_forensic_flags,
    _flag_dilution_velocity,
    _flag_going_concern,
    _flag_leverage_high,
    _flag_ni_ocf_divergence,
    _is_forensic_excluded_sector,
    _load_forensic_flag_overrides,
    reset_forensic_overrides_cache,
)


# ─── Plain-assert runner ──────────────────────────────────────────────────


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    except Exception as exc:  # surface unexpected errors as failures
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


# ─── 1. ni_ocf_divergence ─────────────────────────────────────────────────


def test_ni_ocf_three_consecutive_years_trips():
    """3 FY where NI > OCF in every year → flag True."""
    m = {
        'ni_3y_history': [10.0, 9.0, 8.0],
        'ocf_3y_history': [5.0, 4.0, 3.0],
    }
    assert _flag_ni_ocf_divergence(m) is True


def test_ni_ocf_one_year_resets_streak():
    """2 NI > OCF years then 1 OCF > NI year → flag False (streak resets).

    The 3-year window is hardcoded — if any of the most recent 3 FY has
    OCF >= NI, the all() returns False.
    """
    m = {
        'ni_3y_history':  [10.0, 9.0, 8.0],
        'ocf_3y_history': [5.0, 4.0, 9.0],  # year 3: OCF beats NI
    }
    assert _flag_ni_ocf_divergence(m) is False


def test_ni_ocf_all_years_ocf_higher():
    """All 3 FY OCF > NI → flag False (healthy cash conversion)."""
    m = {
        'ni_3y_history':  [3.0, 4.0, 5.0],
        'ocf_3y_history': [6.0, 8.0, 10.0],
    }
    assert _flag_ni_ocf_divergence(m) is False


def test_ni_ocf_insufficient_history_returns_false():
    """< 3 FY of data → False (not None) so the JSON output is consistent.

    The implementation deliberately returns False rather than None when
    the history is short — see the inline docstring in
    ``_flag_ni_ocf_divergence``.
    """
    m = {'ni_3y_history': [10.0, 9.0], 'ocf_3y_history': [5.0, 4.0]}
    assert _flag_ni_ocf_divergence(m) is False
    m_empty = {'ni_3y_history': [], 'ocf_3y_history': []}
    assert _flag_ni_ocf_divergence(m_empty) is False
    m_missing = {}
    assert _flag_ni_ocf_divergence(m_missing) is False


# ─── 2. leverage_high ─────────────────────────────────────────────────────


def test_leverage_both_legs_trip():
    """Net Debt / EBITDA = 5x AND interest cov = 1.5x → flag True (both trip)."""
    m = {'net_debt': 50.0, 'ebitda_ttm': 10.0, 'interest_coverage_ratio': 1.5}
    # 50/10 = 5 > 4, and 1.5 < 2 → flag
    assert _flag_leverage_high(m) is True


def test_leverage_only_nd_trips():
    """ND/EBITDA = 5x but interest cov = 3x → False (only one leg trips).

    AND threshold — both legs required.
    """
    m = {'net_debt': 50.0, 'ebitda_ttm': 10.0, 'interest_coverage_ratio': 3.0}
    assert _flag_leverage_high(m) is False


def test_leverage_only_icov_trips():
    """ND/EBITDA = 3x with interest cov = 1.5x → False."""
    m = {'net_debt': 30.0, 'ebitda_ttm': 10.0, 'interest_coverage_ratio': 1.5}
    assert _flag_leverage_high(m) is False


def test_leverage_missing_inputs_returns_false():
    """Any None among the three inputs → False (defensive)."""
    assert _flag_leverage_high({}) is False
    assert _flag_leverage_high(
        {'net_debt': None, 'ebitda_ttm': 10.0, 'interest_coverage_ratio': 1.5}
    ) is False
    assert _flag_leverage_high(
        {'net_debt': 50.0, 'ebitda_ttm': None, 'interest_coverage_ratio': 1.5}
    ) is False
    assert _flag_leverage_high(
        {'net_debt': 50.0, 'ebitda_ttm': 10.0, 'interest_coverage_ratio': None}
    ) is False


def test_leverage_negative_net_debt_returns_false():
    """Cash-rich firm (net debt < 0) is undefined for this test → False."""
    m = {'net_debt': -100.0, 'ebitda_ttm': 10.0,
         'interest_coverage_ratio': 1.5}
    assert _flag_leverage_high(m) is False


# ─── 3. dilution_velocity ─────────────────────────────────────────────────


def test_dilution_above_10pct_trips():
    """YoY share growth 12% → True (12 > 10)."""
    assert _flag_dilution_velocity({'shares_outstanding_yoy_growth': 0.12}) is True


def test_dilution_below_10pct_does_not_trip():
    """YoY share growth 8% → False."""
    assert _flag_dilution_velocity({'shares_outstanding_yoy_growth': 0.08}) is False


def test_dilution_boundary_10pct_strict_gt():
    """YoY share growth exactly 10.0% → False.

    Implementation uses ``g > 0.10`` (strict greater-than), not >=. This
    test pins that semantic so a future relax to >= shows up as a failing
    test rather than a silent threshold drift.
    """
    assert _flag_dilution_velocity({'shares_outstanding_yoy_growth': 0.10}) is False


def test_dilution_missing_input_returns_false():
    assert _flag_dilution_velocity({}) is False
    assert _flag_dilution_velocity(
        {'shares_outstanding_yoy_growth': None}
    ) is False


# ─── 4. Sector exclusion (SIC 6000-6799) ──────────────────────────────────


def test_sector_excluded_commercial_bank():
    """SIC 6020 (commercial bank) with NI > OCF for 3 FY → all flags
    suppressed (banks intentionally excluded from this layer)."""
    m = {
        'symbol': 'BAC', 'sic': '6020',
        'ni_3y_history':  [100.0, 90.0, 80.0],
        'ocf_3y_history': [50.0, 40.0, 30.0],
        # Even with a leverage trip handed in, sector exclusion short-
        # circuits before the per-flag computation runs.
        'net_debt': 1000.0, 'ebitda_ttm': 100.0,
        'interest_coverage_ratio': 1.0,
        'shares_outstanding_yoy_growth': 0.20,
    }
    assert _is_forensic_excluded_sector(m) is True
    raw, suppressed, count = _compute_forensic_flags(m)
    assert raw == {}, f"banks should return empty raw map; got {raw}"
    assert count == 0


def test_sector_excluded_life_insurance():
    """SIC 6311 (life insurance) — also excluded (insurance carrier)."""
    m = {'symbol': 'MET', 'sic': '6311'}
    assert _is_forensic_excluded_sector(m) is True


def test_sector_excluded_reit():
    """SIC 6798 (REIT) — excluded; 6798 is the upper bound of the range."""
    m = {'symbol': 'O', 'sic': '6798'}
    assert _is_forensic_excluded_sector(m) is True


def test_sector_not_excluded_software():
    """SIC 7372 (prepackaged software) is OUTSIDE 6000-6799 → not excluded.

    A NI > OCF row should still trip ni_ocf_divergence here.
    """
    m = {
        'symbol': 'TEST', 'sic': '7372',
        'ni_3y_history':  [10.0, 9.0, 8.0],
        'ocf_3y_history': [5.0, 4.0, 3.0],
        'shares_outstanding_yoy_growth': 0.05,
        'net_debt': None, 'ebitda_ttm': None,
        'interest_coverage_ratio': None,
        'going_concern_present': False,
    }
    assert _is_forensic_excluded_sector(m) is False
    raw, suppressed, count = _compute_forensic_flags(m)
    assert raw['ni_ocf_divergence'] is True, (
        f"software firm with NI > OCF for 3 FY should trip; got {raw}")
    assert count == 1


def test_sector_excluded_string_sic():
    """SIC may arrive as either int or string (CSV roundtrip). 6000-6799
    range check coerces via float — '6020' as a string must still exclude."""
    m = {'symbol': 'BAC', 'sic': '6020'}
    assert _is_forensic_excluded_sector(m) is True
    m_int = {'symbol': 'BAC', 'sic': 6020}
    assert _is_forensic_excluded_sector(m_int) is True


# ─── 5. Override behavior ─────────────────────────────────────────────────


def _write_override_csv(tmpdir, rows):
    """Write a forensic-override CSV in the same shape the loader expects.

    rows: iterable of (symbol, flag_name, expires_at_iso, reason) tuples.
    """
    path = os.path.join(tmpdir, 'forensic_flag_overrides.csv')
    with open(path, 'w', newline='', encoding='utf-8') as f:
        f.write('symbol,flag_name,expires_at,reason\n')
        for sym, flag, exp, reason in rows:
            f.write(f'{sym},{flag},{exp},{reason}\n')
    return path


def test_override_active_suppresses_count_keeps_raw():
    """Active override (today < expires_at) → flag stays True in raw map
    but is excluded from forensic_flag_count and added to suppressed set.
    """
    with tempfile.TemporaryDirectory() as tmp:
        future = (date.today() + timedelta(days=365)).isoformat()
        path = _write_override_csv(
            tmp, [('TEST', 'ni_ocf_divergence', future, 'test reason')]
        )
        overrides = _load_forensic_flag_overrides(path)
        assert ('TEST', 'ni_ocf_divergence') in overrides
        m = {
            'symbol': 'TEST', 'sic': '7372',
            'ni_3y_history':  [10.0, 9.0, 8.0],
            'ocf_3y_history': [5.0, 4.0, 3.0],
            'shares_outstanding_yoy_growth': 0.05,
            'net_debt': None, 'ebitda_ttm': None,
            'interest_coverage_ratio': None,
            'going_concern_present': False,
        }
        raw, suppressed, count = _compute_forensic_flags(m, overrides=overrides)
        # Flag stays True in raw map (so UI can show "overridden" state)
        assert raw['ni_ocf_divergence'] is True
        # But it's marked suppressed and excluded from the count
        assert 'ni_ocf_divergence' in suppressed
        assert count == 0, f"override should drop count to 0; got {count}"


def test_override_expired_does_not_suppress():
    """Expired override (today >= expires_at) → flag counts normally."""
    with tempfile.TemporaryDirectory() as tmp:
        past = (date.today() - timedelta(days=1)).isoformat()
        path = _write_override_csv(
            tmp, [('TEST', 'ni_ocf_divergence', past, 'expired')]
        )
        overrides = _load_forensic_flag_overrides(path)
        m = {
            'symbol': 'TEST', 'sic': '7372',
            'ni_3y_history':  [10.0, 9.0, 8.0],
            'ocf_3y_history': [5.0, 4.0, 3.0],
            'shares_outstanding_yoy_growth': 0.05,
            'net_debt': None, 'ebitda_ttm': None,
            'interest_coverage_ratio': None,
            'going_concern_present': False,
        }
        raw, suppressed, count = _compute_forensic_flags(m, overrides=overrides)
        assert raw['ni_ocf_divergence'] is True
        assert 'ni_ocf_divergence' not in suppressed
        assert count == 1, f"expired override should not suppress; got {count}"


def test_override_for_different_ticker_does_not_apply():
    """Override row for another ticker → unaffected."""
    with tempfile.TemporaryDirectory() as tmp:
        future = (date.today() + timedelta(days=365)).isoformat()
        path = _write_override_csv(
            tmp, [('OTHER', 'ni_ocf_divergence', future, 'someone else')]
        )
        overrides = _load_forensic_flag_overrides(path)
        m = {
            'symbol': 'TEST', 'sic': '7372',
            'ni_3y_history':  [10.0, 9.0, 8.0],
            'ocf_3y_history': [5.0, 4.0, 3.0],
            'shares_outstanding_yoy_growth': 0.05,
            'net_debt': None, 'ebitda_ttm': None,
            'interest_coverage_ratio': None,
            'going_concern_present': False,
        }
        raw, suppressed, count = _compute_forensic_flags(m, overrides=overrides)
        assert 'ni_ocf_divergence' not in suppressed
        assert count == 1


def test_override_for_different_flag_does_not_apply():
    """Override row for another flag → that flag suppressed, others count."""
    with tempfile.TemporaryDirectory() as tmp:
        future = (date.today() + timedelta(days=365)).isoformat()
        path = _write_override_csv(
            tmp, [('TEST', 'leverage_high', future, 'pretend leverage override')]
        )
        overrides = _load_forensic_flag_overrides(path)
        m = {
            # ni_ocf trips, leverage doesn't, dilution trips
            'symbol': 'TEST', 'sic': '7372',
            'ni_3y_history':  [10.0, 9.0, 8.0],
            'ocf_3y_history': [5.0, 4.0, 3.0],
            'shares_outstanding_yoy_growth': 0.12,
            'net_debt': None, 'ebitda_ttm': None,
            'interest_coverage_ratio': None,
            'going_concern_present': False,
        }
        raw, suppressed, count = _compute_forensic_flags(m, overrides=overrides)
        # leverage_high override is in the suppressed set (per the loader
        # contract: presence = "would suppress if it tripped"); ni_ocf
        # and dilution still count.
        assert raw['ni_ocf_divergence'] is True
        assert raw['dilution_velocity'] is True
        assert count == 2, (
            f"two unrelated flags should both count; got {count} "
            f"(suppressed={suppressed})")


def test_override_csv_missing_returns_empty_dict():
    """Missing override file → loader returns {} and computation is a no-op."""
    overrides = _load_forensic_flag_overrides(
        path='/nonexistent/path/to/overrides.csv'
    )
    assert overrides == {}


def test_override_csv_only_comments_returns_empty_dict():
    """Override file with only comment lines → empty dict (the canonical
    shipped state of cache/forensic_flag_overrides.csv before any user
    populates it)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'forensic_flag_overrides.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('symbol,flag_name,expires_at,reason\n')
            f.write('# this is a comment-only file, no rows\n')
            f.write('# NVDA,ni_ocf_divergence,2027-01-01,example commented out\n')
        overrides = _load_forensic_flag_overrides(path)
        assert overrides == {}


# ─── 5b. Encoding tolerance (UTF-8 preferred, cp1252 fallback) ────────────


def _override_text_with_emdash(future_iso):
    """Compose a CSV body whose ``reason`` column contains an em-dash.

    Em-dash is the canonical drift case: it's a single byte (0x97) in
    cp1252 but a three-byte sequence (\\xe2\\x80\\x94) in UTF-8. Saving
    a UTF-8 string in Excel on Western Windows rewrites it to cp1252,
    which is exactly the failure mode the loader fix targets.
    """
    return (
        'symbol,flag_name,expires_at,reason\n'
        f'TEST,ni_ocf_divergence,{future_iso},Held — revisit Q1 2027\n'
    )


def test_override_csv_utf8_emdash_round_trips():
    """UTF-8 happy path: reason field with em-dash decodes cleanly and
    the override is loaded. Pins the preferred encoding contract — the
    cp1252 fallback is only for the drift case, not the default."""
    with tempfile.TemporaryDirectory() as tmp:
        future = (date.today() + timedelta(days=365)).isoformat()
        path = os.path.join(tmp, 'forensic_flag_overrides.csv')
        # Path.write_bytes with UTF-8 encoding — what a modern editor
        # would produce.
        with open(path, 'wb') as f:
            f.write(_override_text_with_emdash(future).encode('utf-8'))
        # No fallback warning expected on this path.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            overrides = _load_forensic_flag_overrides(path)
        assert ('TEST', 'ni_ocf_divergence') in overrides, (
            f"UTF-8 em-dash file should load; got {overrides}")
        assert 'cp1252 fallback' not in buf.getvalue(), (
            f"UTF-8 path must NOT emit the fallback warning; "
            f"stderr was: {buf.getvalue()!r}")


def test_override_csv_cp1252_emdash_falls_back_with_warning():
    """cp1252 fallback path: the same content saved as cp1252 (Excel
    default on Western Windows) must load with identical override-map
    shape AND emit the specific fallback warning."""
    with tempfile.TemporaryDirectory() as tmp:
        future = (date.today() + timedelta(days=365)).isoformat()
        path = os.path.join(tmp, 'forensic_flag_overrides.csv')
        # The em-dash byte 0x97 alone is invalid UTF-8 (it's a lone
        # continuation byte) but valid cp1252 — so this file fails the
        # strict UTF-8 decode, then succeeds the cp1252 fallback.
        with open(path, 'wb') as f:
            f.write(_override_text_with_emdash(future).encode('cp1252'))
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            overrides = _load_forensic_flag_overrides(path)
        # Override-map shape is identical to the UTF-8 case.
        assert ('TEST', 'ni_ocf_divergence') in overrides, (
            f"cp1252 fallback should still produce the override; "
            f"got {overrides}")
        # And the operator-facing warning fires so the encoding drift
        # gets noticed at the source.
        stderr = buf.getvalue()
        assert 'cp1252 fallback' in stderr, (
            f"fallback warning string must contain 'cp1252 fallback'; "
            f"stderr was: {stderr!r}")
        assert 'UTF-8' in stderr, (
            f"fallback warning must hint at saving as UTF-8; "
            f"stderr was: {stderr!r}")


def test_override_csv_undecodable_returns_empty_dict():
    """Truly malformed bytes — invalid UTF-8 AND invalid cp1252 — fall
    through to the outer exception handler: warn, return ``{}``,
    screener proceeds with no overrides.

    Python's cp1252 codec is strict about a handful of undefined
    positions (0x81, 0x8D, 0x8F, 0x90, 0x9D). 0x81 is also invalid as
    a leading UTF-8 byte, so this byte fails BOTH decodes — exactly
    the worst-case 'someone pasted from a weird source' scenario.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'forensic_flag_overrides.csv')
        # A header row plus a body byte (0x81) that fails both codecs.
        with open(path, 'wb') as f:
            f.write(b'symbol,flag_name,expires_at,reason\n')
            f.write(bytes([0x81]) + b'\n')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            overrides = _load_forensic_flag_overrides(path)
        assert overrides == {}, (
            f"undecodable file should yield empty dict; got {overrides}")
        assert 'unreadable' in buf.getvalue(), (
            f"outer-handler warning expected; stderr was: "
            f"{buf.getvalue()!r}")


# ─── 6. Going-concern stub ────────────────────────────────────────────────


def test_going_concern_stub_reads_metrics_layer_field_which_is_always_false():
    """The stub-ness lives in fundamental_metrics.py: that module always
    writes ``going_concern_present=False`` (the SubstantialDoubtAboutGoingConcern
    XBRL fact is not exposed by SEC's companyfacts API — verified empirically
    against nine known going-concern filers). ``_flag_going_concern`` is a
    faithful passthrough on that field.

    So in production today the screener flag is always False because the
    metrics layer always feeds it False — pinning that contract from both
    ends. If a future 10-K text-parser starts populating the field with
    real True values, ``_flag_going_concern`` will start tripping without
    a code change here, which is the intended forward-compat path.
    """
    # Default / missing field → False (the most common production case
    # before metrics layer ran)
    assert _flag_going_concern({}) is False
    # Stub-True from metrics — never produced today, but the function
    # would honour it if present. This is the forward-compat hook.
    assert _flag_going_concern({'going_concern_present': True}) is True
    # The actual stubbed value the metrics layer always writes today.
    assert _flag_going_concern({'going_concern_present': False}) is False
    # None coerces to False under bool()
    assert _flag_going_concern({'going_concern_present': None}) is False


def test_going_concern_present_in_raw_flags_map():
    """The schema slot must exist on every non-sector-excluded row, even
    though it always reads False. This is the forward-compat contract:
    when a 10-K text parser lands, the column is already wired into
    forensic_flags_json so consumers don't need a schema migration."""
    m = {
        'symbol': 'TEST', 'sic': '7372',
        'ni_3y_history': [], 'ocf_3y_history': [],
        'shares_outstanding_yoy_growth': None,
        'net_debt': None, 'ebitda_ttm': None,
        'interest_coverage_ratio': None,
        'going_concern_present': False,
    }
    raw, suppressed, count = _compute_forensic_flags(m)
    assert 'going_concern' in raw, (
        f"going_concern key MUST be present in raw flag map "
        f"(even though always False today); got keys: {list(raw.keys())}")
    assert raw['going_concern'] is False
    # All four canonical flag names ship in the raw map.
    expected = {name for name, _fn in FORENSIC_FLAGS}
    assert set(raw.keys()) == expected, (
        f"raw flag map should carry every FORENSIC_FLAGS entry; "
        f"got {set(raw.keys())} expected {expected}")


def test_override_loader_accepts_going_concern_flag_name():
    """The override CSV must accept ``flag_name=going_concern`` even though
    the underlying flag never trips today. This is the forward-compat
    bridge: when 10-K text parsing lands, an existing override row keyed
    on going_concern starts working without a CSV migration.
    """
    with tempfile.TemporaryDirectory() as tmp:
        future = (date.today() + timedelta(days=365)).isoformat()
        path = _write_override_csv(
            tmp, [('FUTURE', 'going_concern', future, 'forward-compat probe')]
        )
        overrides = _load_forensic_flag_overrides(path)
        assert ('FUTURE', 'going_concern') in overrides, (
            f"loader must accept going_concern flag_name even on the "
            f"stub; got {overrides}")


# ─── Runner ────────────────────────────────────────────────────────────────


def run_all():
    fails = 0
    for name, fn in (
        # ni_ocf_divergence
        ("test_ni_ocf_three_consecutive_years_trips",
         test_ni_ocf_three_consecutive_years_trips),
        ("test_ni_ocf_one_year_resets_streak",
         test_ni_ocf_one_year_resets_streak),
        ("test_ni_ocf_all_years_ocf_higher",
         test_ni_ocf_all_years_ocf_higher),
        ("test_ni_ocf_insufficient_history_returns_false",
         test_ni_ocf_insufficient_history_returns_false),
        # leverage_high
        ("test_leverage_both_legs_trip", test_leverage_both_legs_trip),
        ("test_leverage_only_nd_trips", test_leverage_only_nd_trips),
        ("test_leverage_only_icov_trips", test_leverage_only_icov_trips),
        ("test_leverage_missing_inputs_returns_false",
         test_leverage_missing_inputs_returns_false),
        ("test_leverage_negative_net_debt_returns_false",
         test_leverage_negative_net_debt_returns_false),
        # dilution_velocity
        ("test_dilution_above_10pct_trips",
         test_dilution_above_10pct_trips),
        ("test_dilution_below_10pct_does_not_trip",
         test_dilution_below_10pct_does_not_trip),
        ("test_dilution_boundary_10pct_strict_gt",
         test_dilution_boundary_10pct_strict_gt),
        ("test_dilution_missing_input_returns_false",
         test_dilution_missing_input_returns_false),
        # sector exclusion
        ("test_sector_excluded_commercial_bank",
         test_sector_excluded_commercial_bank),
        ("test_sector_excluded_life_insurance",
         test_sector_excluded_life_insurance),
        ("test_sector_excluded_reit", test_sector_excluded_reit),
        ("test_sector_not_excluded_software",
         test_sector_not_excluded_software),
        ("test_sector_excluded_string_sic",
         test_sector_excluded_string_sic),
        # override behaviour
        ("test_override_active_suppresses_count_keeps_raw",
         test_override_active_suppresses_count_keeps_raw),
        ("test_override_expired_does_not_suppress",
         test_override_expired_does_not_suppress),
        ("test_override_for_different_ticker_does_not_apply",
         test_override_for_different_ticker_does_not_apply),
        ("test_override_for_different_flag_does_not_apply",
         test_override_for_different_flag_does_not_apply),
        ("test_override_csv_missing_returns_empty_dict",
         test_override_csv_missing_returns_empty_dict),
        ("test_override_csv_only_comments_returns_empty_dict",
         test_override_csv_only_comments_returns_empty_dict),
        # encoding tolerance (UTF-8 preferred, cp1252 fallback)
        ("test_override_csv_utf8_emdash_round_trips",
         test_override_csv_utf8_emdash_round_trips),
        ("test_override_csv_cp1252_emdash_falls_back_with_warning",
         test_override_csv_cp1252_emdash_falls_back_with_warning),
        ("test_override_csv_undecodable_returns_empty_dict",
         test_override_csv_undecodable_returns_empty_dict),
        # going-concern stub
        ("test_going_concern_stub_reads_metrics_layer_field_which_is_always_false",
         test_going_concern_stub_reads_metrics_layer_field_which_is_always_false),
        ("test_going_concern_present_in_raw_flags_map",
         test_going_concern_present_in_raw_flags_map),
        ("test_override_loader_accepts_going_concern_flag_name",
         test_override_loader_accepts_going_concern_flag_name),
    ):
        # Reset the module-level overrides cache between tests so the
        # production CSV at cache/forensic_flag_overrides.csv (or any
        # earlier test's tmp file) doesn't leak into later cases.
        reset_forensic_overrides_cache()
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
