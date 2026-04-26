"""
classifier.py
─────────────
Three-tier classification (sector → industry_group → industry) derived from
SIC codes plus a small set of ticker-level overrides for mega-caps whose SIC
codes misrepresent their actual business (e.g. AMZN files as SIC 5961 mail-order
catalog, but the user wants AMZN treated as Retail).

Three tiers:
    sector          — broad navigation, 10 values.
    industry_group  — peer-median benchmarking unit, 29 values, all ≥5 members
                      across the 174-ticker universe. "Interactive Media &
                      Services" (a 3-ticker group containing GOOGL/META/NFLX)
                      is merged into "Telecom & Media" so the peer pool is
                      large enough for a meaningful median, while the more
                      granular ``industry`` field preserves "Interactive Media"
                      so the verdict card can label these tickers honestly.
    industry        — finer display label. For most SIC codes
                      ``industry == industry_group``; for the override tickers
                      they may differ.

Public API:
    classify(symbol, sic, sic_description) -> (sector, industry_group, industry)
    TICKER_OVERRIDES                       -> module-level constant dict

Leaf module — imports only the standard library. No imports from any other
module in this repo, so it can be re-used freely from screener / verdict /
frontend-data paths without coupling concerns.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Optional, Tuple, Union

# ─── Ticker-level overrides ──────────────────────────────────────────────────
# Mega-caps whose registered SIC code does not reflect their actual business.
# Edit-once, edit-here: adding a new override is a one-line change. Lookup is
# O(1). Symbols are normalized to upper-case before this dict is consulted.
#
# Rationale per ticker (kept inline so future editors see the "why" beside
# the "what"):
#   GOOGL/GOOG/META/NFLX — digital-media giants. industry_group is
#       "Telecom & Media" so they sit in the 34-ticker peer pool for median
#       benchmarking; industry is "Interactive Media" so the verdict card
#       still labels their actual business honestly.
#   AMZN  — files as mail-order catalog (SIC 5961); user-classified Retail.
#   AAPL  — files as electronic computers (SIC 3571); user-classified
#           Tech Hardware & Networking.
#   TSLA  — files as Motor Vehicles (SIC 3711); already in the right SIC
#           bucket but we pin the exact triple here so the labels can't drift.
#   V/MA  — payment networks. SIC 6199 ("Finance Services") is too generic;
#           pin to Capital Markets / Payments.
TICKER_OVERRIDES: dict[str, Tuple[str, str, str]] = {
    "GOOGL": ("Communication Services", "Telecom & Media", "Interactive Media"),
    "GOOG":  ("Communication Services", "Telecom & Media", "Interactive Media"),
    "META":  ("Communication Services", "Telecom & Media", "Interactive Media"),
    "NFLX":  ("Communication Services", "Telecom & Media", "Interactive Media"),

    "AMZN":  ("Consumer Discretionary", "Retail & Restaurants", "Retail"),
    "AAPL":  ("Technology", "Hardware & Equipment", "Tech Hardware & Networking"),
    "TSLA":  ("Consumer Discretionary", "Autos & Components", "Automobiles & Components"),

    "V":     ("Financials", "Capital Markets", "Payments"),
    "MA":    ("Financials", "Capital Markets", "Payments"),
}


# ─── SIC range table ─────────────────────────────────────────────────────────
# Each row is (lo, hi, (sector, industry_group, industry)). Ranges MUST be
# sorted by ``lo`` and disjoint — enforced by the import-time invariant check
# below. Adjacent ranges (hi == lo_next - 1) are fine and used routinely
# (e.g. 2833 → Chemicals, 2834-2836 → Pharmaceuticals, 2837 → Chemicals).
#
# Reading guide: groups are roughly ordered by SIC division (agriculture,
# mining, construction, manufacturing, transport/utilities/comms, wholesale,
# retail, finance, services). Industry name is set per-row so SIC sub-ranges
# inside a single industry_group can carry distinct industry labels.
SIC_RANGES: list[Tuple[int, int, Tuple[str, str, str]]] = [
    # Agriculture, Forestry, Fishing
    ( 100,  999, ("Consumer Staples", "Agriculture & Agricultural Products", "Agriculture & Agricultural Products")),

    # Metal Mining
    (1000, 1099, ("Materials", "Metals/Mining/Steel", "Metals/Mining/Steel")),

    # Coal Mining + Oil & Gas Extraction (E&P + field services live here)
    (1200, 1299, ("Energy", "Oil, Gas & Coal E&P", "Coal Mining")),
    # SIC 1311 (Crude Petroleum & Natural Gas), 1381/1382/1389 (drilling +
    # field services). The user's test_classify_by_sic_oil_gas_ep pins
    # SIC 1311 → ("Energy", "Oil, Gas & Coal E&P", "Services").
    (1300, 1399, ("Energy", "Oil, Gas & Coal E&P", "Services")),

    # Nonmetallic Minerals (excluding fuels) — bucketed with metals/mining
    (1400, 1499, ("Materials", "Metals/Mining/Steel", "Metals/Mining/Steel")),

    # Construction
    (1500, 1799, ("Industrials", "Construction & Engineering", "Construction & Engineering")),

    # Food & Kindred Products + Tobacco
    (2000, 2099, ("Consumer Staples", "Food/Beverage/Tobacco", "Food/Beverage/Tobacco")),
    (2100, 2199, ("Consumer Staples", "Food/Beverage/Tobacco", "Food/Beverage/Tobacco")),

    # Textile Mill + Apparel
    (2200, 2399, ("Consumer Discretionary", "Apparel/Leisure Goods/Home Furnishings", "Apparel/Leisure Goods/Home Furnishings")),

    # Lumber & Wood Products
    (2400, 2499, ("Materials", "Paper/Packaging/Building Materials", "Paper/Packaging/Building Materials")),

    # Furniture & Fixtures
    (2500, 2599, ("Consumer Discretionary", "Apparel/Leisure Goods/Home Furnishings", "Apparel/Leisure Goods/Home Furnishings")),

    # Paper & Allied Products
    (2600, 2699, ("Materials", "Paper/Packaging/Building Materials", "Paper/Packaging/Building Materials")),

    # Printing, Publishing & Allied Industries — Communication Services
    (2700, 2799, ("Communication Services", "Telecom & Media", "Publishing")),

    # Chemicals — split: 2800-2833 base chemicals, 2834-2836 pharma,
    # 2837-2839 misc chemicals, 2840-2844 soaps/HPP, 2845-2899 misc chemicals.
    (2800, 2833, ("Materials", "Chemicals", "Chemicals")),
    (2834, 2836, ("Healthcare", "Pharmaceuticals", "Pharmaceuticals")),
    (2837, 2839, ("Materials", "Chemicals", "Chemicals")),
    (2840, 2844, ("Consumer Staples", "Household & Personal Products", "Household & Personal Products")),
    (2845, 2899, ("Materials", "Chemicals", "Chemicals")),

    # Petroleum Refining (2911 etc.)
    (2900, 2999, ("Energy", "Oil & Gas Refining/Midstream", "Petroleum Refining")),

    # Rubber & Misc Plastics
    (3000, 3099, ("Materials", "Chemicals", "Chemicals")),

    # Leather & Leather Products
    (3100, 3199, ("Consumer Discretionary", "Apparel/Leisure Goods/Home Furnishings", "Apparel/Leisure Goods/Home Furnishings")),

    # Stone, Clay, Glass, Concrete
    (3200, 3299, ("Materials", "Paper/Packaging/Building Materials", "Paper/Packaging/Building Materials")),

    # Primary Metal + Fabricated Metal
    (3300, 3499, ("Materials", "Metals/Mining/Steel", "Metals/Mining/Steel")),

    # Industrial & Commercial Machinery — split: 3570-3579 Computer/Office equip → Tech.
    (3500, 3569, ("Industrials", "Machinery & Equipment", "Machinery & Equipment")),
    (3570, 3579, ("Technology", "Hardware & Equipment", "Tech Hardware & Networking")),
    (3580, 3599, ("Industrials", "Machinery & Equipment", "Machinery & Equipment")),

    # Electronic & Electrical Equipment — split: 3600-3659 industrial electrical;
    # 3660-3669 communications equip → Tech Hardware; 3670-3673 electronic comp →
    # Tech Hardware; 3674 → Semiconductors; 3675-3699 misc electronic → Tech Hardware.
    (3600, 3659, ("Industrials", "Electrical Equipment", "Electrical Equipment")),
    (3660, 3673, ("Technology", "Hardware & Equipment", "Tech Hardware & Networking")),
    (3674, 3674, ("Technology", "Semiconductors", "Semiconductors")),
    (3675, 3699, ("Technology", "Hardware & Equipment", "Tech Hardware & Networking")),

    # Transportation Equipment — split: 3700-3719 motor vehicles → Autos;
    # 3720-3729 aircraft → Aero/Defense; 3730-3759 ships/railroad eqpt → Transport;
    # 3760-3769 missiles/space → Aero/Defense; 3770-3799 motorcycles/bikes/trailers → Autos.
    (3700, 3719, ("Consumer Discretionary", "Autos & Components", "Automobiles & Components")),
    (3720, 3729, ("Industrials", "Aerospace & Defense", "Aerospace & Defense")),
    (3730, 3759, ("Industrials", "Transportation & Logistics", "Transportation & Logistics")),
    (3760, 3769, ("Industrials", "Aerospace & Defense", "Aerospace & Defense")),
    (3770, 3799, ("Consumer Discretionary", "Autos & Components", "Automobiles & Components")),

    # Measuring/Controlling/Lab Instruments + Photographic — split: 3840-3849 medical → Healthcare.
    (3800, 3839, ("Industrials", "Machinery & Equipment", "Machinery & Equipment")),
    (3840, 3849, ("Healthcare", "Medical Devices & Instruments", "Medical Devices & Instruments")),
    (3850, 3899, ("Industrials", "Machinery & Equipment", "Machinery & Equipment")),

    # Misc Manufacturing — jewelry, toys, sporting goods, etc.
    (3900, 3999, ("Consumer Discretionary", "Apparel/Leisure Goods/Home Furnishings", "Apparel/Leisure Goods/Home Furnishings")),

    # Transportation services — railroad, trucking, water, transit
    (4000, 4299, ("Industrials", "Transportation & Logistics", "Transportation & Logistics")),
    # Note: 4300-4399 is unassigned in SIC; covered by gap (returns Unknown).
    (4400, 4599, ("Industrials", "Transportation & Logistics", "Transportation & Logistics")),

    # Pipelines (4612 oil pipelines, etc.) — Energy/Midstream
    (4600, 4699, ("Energy", "Oil & Gas Refining/Midstream", "Pipelines")),

    # Transportation Services
    (4700, 4799, ("Industrials", "Transportation & Logistics", "Transportation & Logistics")),

    # Communications (4812 wireless, 4813 telephone, 4832-4833 broadcasting,
    # 4841 cable TV, 4899 misc comms) → Telecom & Media
    (4800, 4899, ("Communication Services", "Telecom & Media", "Telecom")),

    # Electric, Gas & Sanitary Services — split: 4920-4929 natural gas
    # transmission/distribution → Energy/Midstream (gas pipeline operators).
    (4900, 4919, ("Utilities", "Electric & Other Utilities", "Electric & Other Utilities")),
    (4920, 4929, ("Energy", "Oil & Gas Refining/Midstream", "Natural Gas Distribution")),
    (4930, 4999, ("Utilities", "Electric & Other Utilities", "Electric & Other Utilities")),

    # Wholesale Trade
    (5000, 5199, ("Industrials", "Wholesale Trade", "Wholesale Trade")),

    # Retail Trade — split: 5800-5899 eating/drinking → Hotels/Restaurants/Leisure.
    (5200, 5799, ("Consumer Discretionary", "Retail & Restaurants", "Retail")),
    (5800, 5899, ("Consumer Discretionary", "Hotels/Restaurants/Leisure", "Restaurants")),
    (5900, 5999, ("Consumer Discretionary", "Retail & Restaurants", "Retail")),

    # Finance, Insurance, Real Estate
    (6000, 6299, ("Financials", "Capital Markets", "Banking & Capital Markets")),
    (6300, 6499, ("Financials", "Insurance", "Insurance")),
    (6500, 6799, ("Financials", "Capital Markets", "Real Estate & Investment")),

    # Services — Hotels & Lodging
    (7000, 7039, ("Consumer Discretionary", "Hotels/Restaurants/Leisure", "Hotels & Lodging")),

    # Personal Services
    (7200, 7299, ("Industrials", "Professional & Commercial Services", "Professional & Commercial Services")),

    # Business Services — split: 7370-7379 Computer Services → Tech/Software.
    (7300, 7369, ("Industrials", "Professional & Commercial Services", "Professional & Commercial Services")),
    (7370, 7379, ("Technology", "Software & IT Services", "Software & IT Services")),
    (7380, 7399, ("Industrials", "Professional & Commercial Services", "Professional & Commercial Services")),

    # Auto Repair / Misc Repair Services
    (7500, 7699, ("Industrials", "Professional & Commercial Services", "Professional & Commercial Services")),

    # Motion Pictures → Communication Services
    (7800, 7899, ("Communication Services", "Telecom & Media", "Motion Pictures")),

    # Amusement & Recreation Services
    (7900, 7999, ("Consumer Discretionary", "Hotels/Restaurants/Leisure", "Amusement & Recreation")),

    # Health Services
    (8000, 8099, ("Healthcare", "Healthcare Services", "Healthcare Services")),

    # Educational, Social, Membership, Engineering, Accounting, Mgmt Services
    (8100, 8999, ("Industrials", "Professional & Commercial Services", "Professional & Commercial Services")),
]

# Parallel array of low keys for bisect lookup. Kept separate from SIC_RANGES
# so bisect_right can compare scalars (avoids the tuple-comparison footgun
# wright flagged in Phase 1 review — sorting by full tuple would let payload
# strings reorder rows on ties).
_LO_KEYS: list[int] = [r[0] for r in SIC_RANGES]


# ─── Import-time invariant check ─────────────────────────────────────────────
# Ranges must be sorted by ``lo`` AND disjoint. A future PR that adds an
# overlapping or out-of-order row makes ``import classifier`` fail loudly,
# which cascades to every test and pipeline run that imports this module —
# strictly stronger than catching the regression in a unit test.
for _i in range(len(SIC_RANGES) - 1):
    _lo_i, _hi_i, _ = SIC_RANGES[_i]
    _lo_next, _, _ = SIC_RANGES[_i + 1]
    if not (_lo_i <= _hi_i < _lo_next):
        raise ValueError(
            f"SIC_RANGES invariant violated at index {_i}: "
            f"({_lo_i}, {_hi_i}) vs lo_next={_lo_next}. "
            f"Ranges must be sorted by lo and disjoint."
        )
del _i, _lo_i, _hi_i, _lo_next


# ─── Public function ─────────────────────────────────────────────────────────
_UNKNOWN = ("Unknown", "Unknown", "Unknown")


def _coerce_sic(sic: Union[str, int, None]) -> Optional[int]:
    """Return the SIC as an int, or None if it cannot be parsed.

    Handles the three shapes we see in the wild:
      - int from XBRL fact rows
      - str ("1311") or padded str ("01311") from CSV
      - "1311.0" from a stray pandas/yfinance round-trip
      - None / empty string / non-numeric → None
    """
    if sic is None:
        return None
    try:
        return int(float(sic))
    except (TypeError, ValueError):
        return None


def classify(
    symbol: Union[str, None],
    sic: Union[str, int, None],
    sic_description: Union[str, None] = None,
) -> Tuple[str, str, str]:
    """Return (sector, industry_group, industry) for a ticker.

    Resolution order:
      1. ``TICKER_OVERRIDES`` lookup on upper-cased symbol (mega-cap pins).
      2. ``SIC_RANGES`` lookup via bisect (most companies).
      3. Fall through to ``("Unknown", "Unknown", "Unknown")`` for None /
         empty / non-numeric SIC, or ``("Unknown", "Unknown", f"SIC {n}")``
         for a numeric SIC that doesn't match any range.

    Args:
        symbol: ticker string. ``None`` or empty falls through to SIC lookup.
        sic: SIC code as int (XBRL) or str (CSV). ``None`` / empty / non-
             numeric returns the unknown triple.
        sic_description: accepted for forward compatibility with future
             tie-breakers (e.g. disambiguating SIC 6199 "Finance Services"
             via description keywords). Currently unused — kept in the
             signature so callers don't need to change later.

    Returns:
        3-tuple of non-empty strings.
    """
    # Step 1: ticker override.
    if symbol:
        key = symbol.strip().upper()
        if key in TICKER_OVERRIDES:
            return TICKER_OVERRIDES[key]

    # Step 2: SIC range lookup.
    sic_int = _coerce_sic(sic)
    if sic_int is None:
        return _UNKNOWN

    # bisect_right - 1 yields the rightmost range whose lo <= sic_int.
    # Then verify hi >= sic_int (the range may not actually cover the value
    # if sic falls in a gap between two adjacent ranges).
    idx = bisect_right(_LO_KEYS, sic_int) - 1
    if idx >= 0:
        lo, hi, classification = SIC_RANGES[idx]
        if lo <= sic_int <= hi:
            return classification

    # Step 3: numeric SIC, but no range covers it.
    return ("Unknown", "Unknown", f"SIC {sic_int}")
