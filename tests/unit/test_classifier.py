"""Unit tests for ``classifier.classify`` — the 9 cases from
ITERATION_PLAN_V2.md Round 7c.

Plain-assert style, matching the rest of ``tests/unit/``. Each test is one
assertion; the suite has no fixtures, no I/O, no mocking — the classifier
is a pure function over (symbol, sic, sic_description).
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from classifier import classify


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


# 1. AAPL ticker override → Tech Hardware & Networking.
def test_classify_apple_is_tech_hardware_and_networking():
    assert classify("AAPL", 3571, "Electronic Computers") == (
        "Technology",
        "Hardware & Equipment",
        "Tech Hardware & Networking",
    )


# 2. GOOGL ticker override — validates that industry_group ("Telecom & Media")
#    differs from industry ("Interactive Media"). This is the documented
#    "two-tier granularity" case the spec calls out.
def test_classify_alphabet_industry_group_is_telecom_media_industry_is_interactive_media():
    sector, group, industry = classify("GOOGL", 7370, "Computer Services")
    assert sector == "Communication Services"
    assert group == "Telecom & Media"
    assert industry == "Interactive Media"


# 3. AMZN ticker override → Retail.
def test_classify_amazon_is_retail():
    assert classify("AMZN", 5961, "Catalog, Mail-Order Houses") == (
        "Consumer Discretionary",
        "Retail & Restaurants",
        "Retail",
    )


# 4. TSLA ticker override → Autos & Components.
def test_classify_tesla_is_autos_and_components():
    sector, group, industry = classify("TSLA", 3711, "Motor Vehicles & Passenger Car Bodies")
    assert sector == "Consumer Discretionary"
    assert group == "Autos & Components"
    assert industry == "Automobiles & Components"


# 5. SIC 2834 (Pharmaceutical Preparations) — non-overridden ticker resolves
#    via the SIC range table to Healthcare / Pharmaceuticals, with the SEC
#    SIC description surfaced as the third (industry) tier.
def test_classify_by_sic_pharma():
    assert classify("PFE", 2834, "Pharmaceutical Preparations") == (
        "Healthcare",
        "Pharmaceuticals",
        "Pharmaceutical Preparations",
    )


# 6. SIC 1311 (Crude Petroleum & Natural Gas) — three-tier path showing that
#    sector, industry_group, and industry are all distinct for the energy
#    E&P group: Industry now carries the SEC SIC description ("what the
#    company does") rather than the fallback "Services" bucket label.
def test_classify_by_sic_oil_gas_ep():
    assert classify("XOM", 1311, "Crude Petroleum and Natural Gas") == (
        "Energy",
        "Oil, Gas & Coal E&P",
        "Crude Petroleum and Natural Gas",
    )


# 7. Unmapped numeric SIC — returns ("Unknown", "Unknown", "SIC <n>") so the
#    UI can still render something diagnostic.
def test_classify_unclassified_sic_returns_unknown():
    # SIC 9999 falls past all defined ranges (last range ends at 8999).
    assert classify("ZZZZ", 9999, "Public Administration") == (
        "Unknown",
        "Unknown",
        "SIC 9999",
    )


# 8. Determinism — same inputs, same outputs, no hidden state.
def test_classify_is_deterministic_for_same_input():
    a = classify("PFE", 2834, "Pharmaceutical Preparations")
    b = classify("PFE", 2834, "Pharmaceutical Preparations")
    c = classify("PFE", 2834, "Pharmaceutical Preparations")
    assert a == b == c


# 9. None / empty SIC — robustness check; returns the all-Unknown triple.
def test_classify_null_sic_returns_unknown():
    # Each of None, "", and a non-numeric string falls into the Unknown branch.
    assert classify("UNKWN", None, None) == ("Unknown", "Unknown", "Unknown")
    assert classify("UNKWN", "", "") == ("Unknown", "Unknown", "Unknown")
    assert classify("UNKWN", "not-a-sic", None) == ("Unknown", "Unknown", "Unknown")


# 10. SIC matches a range but sic_description is missing — Industry tier
#     falls back to industry_group so the field is always populated.
def test_classify_industry_falls_back_to_industry_group_when_sic_description_missing():
    sector, industry_group, industry = classify("XYZ", "2834", None)
    assert industry == industry_group


def run_all() -> int:
    fails = 0
    for name, fn in (
        ("test_classify_apple_is_tech_hardware_and_networking",
         test_classify_apple_is_tech_hardware_and_networking),
        ("test_classify_alphabet_industry_group_is_telecom_media_industry_is_interactive_media",
         test_classify_alphabet_industry_group_is_telecom_media_industry_is_interactive_media),
        ("test_classify_amazon_is_retail",
         test_classify_amazon_is_retail),
        ("test_classify_tesla_is_autos_and_components",
         test_classify_tesla_is_autos_and_components),
        ("test_classify_by_sic_pharma",
         test_classify_by_sic_pharma),
        ("test_classify_by_sic_oil_gas_ep",
         test_classify_by_sic_oil_gas_ep),
        ("test_classify_unclassified_sic_returns_unknown",
         test_classify_unclassified_sic_returns_unknown),
        ("test_classify_is_deterministic_for_same_input",
         test_classify_is_deterministic_for_same_input),
        ("test_classify_null_sic_returns_unknown",
         test_classify_null_sic_returns_unknown),
        ("test_classify_industry_falls_back_to_industry_group_when_sic_description_missing",
         test_classify_industry_falls_back_to_industry_group_when_sic_description_missing),
    ):
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
