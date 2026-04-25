# Quantfolio — Iteration Plan V2

**Scope:** the next four rounds (7a through 7d), derived from user feedback after real use of Quantfolio through the Round 6 shipped state.

**Starting point:** `main` at the post-Round-5/6 merge. Code pointers re-checked against HEAD on 2026-04-24.

**Prior context:**
- `ITERATION_PLAN.md` (now complete) covered Rounds 4-6 — reliable data fetching, stale-feature warning, Pro model banner.
- `NEXT_ROUNDS.md` is the long-range backlog memo.
- `FEATURE_BACKLOG.md` has FB-1 through FB-8 (this plan's scope maps to most of them).
- `PATTERNS.md` P-1 (respect `[hidden]` in CSS display rules) — reference in any prompt that adds a new UI element with default-hidden state.

**Out of scope** (tracked but not in this plan):
- H-2, H-4, H-5 — deferred per NEXT_ROUNDS.md
- ML methodology review — separate iteration
- Security audit — separate iteration

---

## Shape of this iteration

Seven user items plus one diagnosed issue (SVR transparency, became FB-4). Grouped by dependency and pattern:

- **Items 2 and 6** share the "expand inline near clicked row" pattern → single round.
- **Items 4 and 5** are small Daily Report polish → single round.
- **Items 1 and 7** are data-pipeline coupled → two rounds (data infrastructure, then display).
- **Item 3** turned out to be UI transparency → folded into Round 7d as FB-4.

Four rounds:

| Round | Scope | Effort | Risk |
|---|---|---|---|
| 7a | Inline expansion for verdict card + strategy comparison (FB-2, FB-8) | 2-3 h | Low |
| 7b | Daily Report polish (FB-6, FB-7) | 1-2 h | Low |
| 7c | Classifier module + Industry Group filter (FB-1 data half, FB-5) | 3-4 h | Medium |
| 7d | Peer benchmarking column + timestamp chip (FB-1 display half, FB-4) | 3-4 h | Medium |

**Total:** 9-13 hours. 7a/7b are low-risk frontend wins; 7c lays data infrastructure; 7d consumes it.

---

## Round 7a — Inline expansion for verdict card + strategy comparison

**User intent:** Stop losing scroll position. When I click a ticker row, show detail near the row, not at a fixed location on the page.

**Scope:**

**A. Verdict card (FB-2):**
- Currently renders at fixed top of each tab.
- On Daily Report and Leader Detector, clicking a ticker row expands the verdict card inline beneath that row. Ticker Lookup keeps its current near-the-Predict-button placement.
- Multi-click: clicking a second row collapses the first card, expands the second. One open at a time.
- Clicked row auto-scrolls into view after expansion.

**B. Strategy Lab comparison chart (FB-8):**
- Currently renders at bottom of Strategy Lab.
- Expand inline beneath the clicked library row. Same behavior model as verdict card.

**Files:** `frontend/index.html` only.

**Constraints:**
- Below ~640px viewport, fall back to modal overlay (piggyback on existing responsive breakpoints).
- Preserve keyboard accessibility — focus moves to expanded card.
- **Reference PATTERNS.md P-1** if new default-hidden elements added.

**Commits:** Two.
1. `feat: verdict card expands inline beneath clicked row on Daily Report and Leader Detector (FB-2)`
2. `feat: strategy comparison chart expands inline beneath clicked library row (FB-8)`

**Verification:** Manual click-through on all affected surfaces. Hard-refresh after each commit.

**Team:** Single agent (sophia).

---

## Round 7b — Daily Report polish

**User intent:** Banner should be honest about as-of dates; table shouldn't repeat information; Strategy Lab should default to the symbols I actually care about.

**Scope:**

**A. Strategy Lab default (FB-6):**
- Strategy Lab defaults to showing only symbols that appear in the current Daily Report.
- If no Daily Report cached, falls back to current default with notice: "No Daily Report data yet — showing all cached backtest symbols. Run Daily Report first for a focused list."
- Frontend-only client-side filter.

**B. Remove per-row "As of" column (FB-7 part 1):**
- Redundant information when most rows share the same date.

**C. Daily Report banner improvement (FB-7 part 2):**
- Current: `Report generated: <Date, Time> | <X> symbols analyzed`
- New (multi-date): `Report generated: <Date, Time> | <X> symbols analyzed | <N1> symbols' close price as of <Date1>, <N2> symbols' close price as of <Date2>`
- New (single-date): `Report generated: <Date, Time> | <X> symbols analyzed | Close prices as of <Date>`
- Data already in `/api/report`; client aggregates.

**Files:** `frontend/index.html` only.

**Commits:** Two.
1. `feat: Daily Report banner aggregates per-date close prices; remove redundant per-row column (FB-7)`
2. `feat: Strategy Lab defaults to Daily Report symbols (FB-6)`

**Verification:**
- Mixed-date set: banner shows split.
- Single-date set: banner shows unified text.
- Strategy Lab with/without cached Daily Report: behavior correct.

**Team:** Single agent (skipper).

---

## Round 7c — Classifier module + Industry Group filter

**User intent:** Codify the three-tier classification (sector → industry_group → industry) as Python logic, not a CSV. Surface industry_group as a filter.

**Depends on:** Nothing. Can run independently of 7a/7b.

**Scope:**

**A. New module `classifier.py` — pure function, no app dependencies:**

```python
def classify(symbol: str, sic: str | int | None, sic_description: str | None) -> tuple[str, str, str]:
    """Returns (sector, industry_group, industry).
    
    Three-tier hierarchy:
      - sector: broad navigation (10 sectors)
      - industry_group: used for peer-median benchmarking (29 groups, all >=5 members)
      - industry: finer display label
    
    Rules:
      1. Ticker-level overrides first (TICKER_OVERRIDES dict).
      2. Otherwise, map SIC -> (sector, industry_group, industry) via rule table.
      3. Unclassified SIC returns ("Unknown", "Unknown", f"SIC {sic}").
    """
```

**Ticker overrides (canonical — matches user's classification decisions):**

```python
TICKER_OVERRIDES = {
    # Digital media giants — industry_group = Telecom & Media for peer math
    # (34-ticker pool), industry = Interactive Media preserves business granularity.
    "GOOGL": ("Communication Services", "Telecom & Media", "Interactive Media"),
    "GOOG":  ("Communication Services", "Telecom & Media", "Interactive Media"),
    "META":  ("Communication Services", "Telecom & Media", "Interactive Media"),
    "NFLX":  ("Communication Services", "Telecom & Media", "Interactive Media"),

    # Mega-caps with misleading SIC codes.
    "AMZN":  ("Consumer Discretionary", "Retail & Restaurants", "Retail"),
    "AAPL":  ("Technology", "Hardware & Equipment", "Tech Hardware & Networking"),
    "TSLA":  ("Consumer Discretionary", "Autos & Components", "Automobiles & Components"),

    # Payment networks in Financials.
    "V":     ("Financials", "Capital Markets", "Payments"),
    "MA":    ("Financials", "Capital Markets", "Payments"),
}
```

**10 sectors:** Communication Services, Consumer Discretionary, Consumer Staples, Energy, Financials, Healthcare, Industrials, Materials, Technology, Utilities.

**29 industry groups** (all ≥5 members — user merged "Interactive Media & Services" 3-ticker group into Telecom & Media for peer math viability): Telecom & Media, Hotels/Restaurants/Leisure, Retail & Restaurants, Autos & Components, Apparel/Leisure Goods/Home Furnishings, Hardware & Equipment, Software & IT Services, Semiconductors, Pharmaceuticals, Medical Devices & Instruments, Healthcare Services, Capital Markets, Insurance, Food/Beverage/Tobacco, Household & Personal Products, Oil/Gas/Coal E&P/Services, Oil & Gas Refining/Midstream, Chemicals, Metals/Mining/Steel, Paper/Packaging/Building Materials, Machinery & Equipment, Aerospace & Defense, Transportation & Logistics, Professional & Commercial Services, Construction & Engineering, Wholesale Trade, Electric & Other Utilities, Electrical Equipment, Agriculture & Agricultural Products.

**B. Pipeline integration:**
- `fundamental_screener.py` — call `classify()` when writing `screener_results.csv`; add `sector`, `industry_group`, `industry` columns. Existing columns unchanged.
- `verdict_provider.py` — include classification fields in the verdict response.
- `leader_selector.py` — fields available but no behavior change this round.

**C. Industry Group filter on Leader Detector (FB-5):**
- Add Industry Group filter chip row below the existing Sector chip row.
- Filters are AND. Selecting Sector=Technology + Industry Group=Semiconductors narrows correctly.
- Each Industry Group chip shows count within the current filtered view.

**Files:**
- `classifier.py` (new)
- `tests/unit/test_classifier.py` (new, 9 tests)
- `fundamental_screener.py` (add columns)
- `verdict_provider.py` (pass through)
- `frontend/index.html` (filter chips)
- `DEVELOPMENT.md` (document in §2)

**Tests (9 cases):**
- `test_classify_apple_is_tech_hardware_and_networking` — AAPL override
- `test_classify_alphabet_industry_group_is_telecom_media_industry_is_interactive_media` — GOOGL override, validates two-tier granularity
- `test_classify_amazon_is_retail` — AMZN override
- `test_classify_tesla_is_autos_and_components` — TSLA override
- `test_classify_by_sic_pharma` — SIC 2834 → Healthcare / Pharmaceuticals
- `test_classify_by_sic_oil_gas_ep` — SIC 1311 → Energy / Oil, Gas & Coal E&P / Services
- `test_classify_unclassified_sic_returns_unknown` — unmapped SIC returns ("Unknown", "Unknown", "SIC <N>")
- `test_classify_is_deterministic_for_same_input` — idempotent
- `test_classify_null_sic_returns_unknown` — robust to None/empty

**Constraints:**
- `classifier.py` has zero imports from app modules. Leaf module.
- Ticker override dict is a module-level constant, not a config file.
- `fundamental_screener.py` must emit existing columns unchanged. Additive only.
- For most SIC codes, `industry_group == industry` (e.g., Semiconductors). For override tickers, they may differ (GOOGL has Telecom & Media / Interactive Media). Downstream code treats them as independent fields.

**Commits:** Three.
1. `feat: classifier module with SIC + ticker-override rules, 10 sectors and 29 industry groups (FB-1 data half)`
2. `feat: fundamental_screener writes sector/industry_group/industry; verdict_provider surfaces fields`
3. `feat: Industry Group filter chips on Leader Detector tab (FB-5)`

**Verification:**
- All 50 tests pass (41 existing + 9 new classifier tests).
- Re-run `leader_selector.py --build` to regenerate `leaders.csv` with new fields.
- Manual: Leader Detector filter chips work; selecting Technology + Semiconductors gives expected count.
- Manual: search for GOOGL in screener data, confirm `industry_group = "Telecom & Media"` and `industry = "Interactive Media"`.

**Team:** Three-agent.
- **wright** reviews `classifier.py` API design before code is written.
- **skipper** writes module, tests, pipeline integration.
- **sophia** reviews Industry Group filter UX.

**Risk and mitigation:**
- Classification drift from override mistakes → 9 unit tests pin canonical overrides.
- `fundamental_screener.py` backward compat → existing columns unchanged (constraint above).
- GOOGL/META/NFLX peer medians distorted by traditional media in Telecom & Media → "Industry: Interactive Media" label signals this. No silent treatment.

---

## Round 7d — Peer benchmarking column + verdict card timestamp

**User intent:** Make the verdict card self-explaining (peer context) and honest about data freshness (timestamp).

**Depends on:** Round 7c (industry_group field must exist on screener rows).

**Scope:**

**A. Peer benchmark column (FB-1 display):**
- Verdict card gets a third column: peer median for the same industry_group.
- Metrics with peers: Revenue YoY, Revenue 3Y CAGR, Gross Margin (TTM), Operating Margin, Rule of 40, ROIC (TTM), SVR, Sector Rank.
- Metrics without peers (no meaningful median): Revenue (TTM), Operating Cash Flow, Free Cash Flow, Growth Trajectory (categorical), Shares 3Y Growth, Sector (label itself).
- With 29 industry groups all ≥5 members, no "insufficient peers" case to handle.

**B. Unified verdict card across all three tabs:**
- Ticker Lookup, Daily Report, Leader Detector → identical card shape and data.
- Audit the three rendering paths, consolidate to one function reading from `verdict_provider`.

**C. Verdict card timestamp chip (FB-4):**
- Small "As of YYYY-MM-DD" chip in the card header.
- Value from `screener_results.csv` mtime, or explicit `snapshot_date` field if exists, else `fundamentals.db` last-refresh.
- Reuse existing "CACHED" chip CSS family from the big SVR card.

**Files:**
- `verdict_provider.py` — peer median computation, snapshot_date.
- `frontend/index.html` — card rendering consolidation, peer column, timestamp chip.
- `tests/unit/test_verdict_provider.py` (new or extended) — 4 tests.

**Tests (4 cases):**
- `test_peer_median_returns_median_of_industry_group` — fixture with semiconductor rows, verify median.
- `test_peer_median_handles_missing_metric` — sparse data: median of what's present; all-missing returns None.
- `test_peer_median_uses_industry_group_not_sector` — GOOGL gets Telecom & Media peers, not all of Communication Services.
- `test_snapshot_date_from_csv_mtime` — fallback when no explicit snapshot field.

**Constraints:**
- Cache peer medians in-memory at `verdict_provider` load. Invalidate on `screener_results.csv` mtime change.
- Timestamp chip and peer medians pull from the same source (one snapshot).
- **Reference PATTERNS.md P-1** for any new default-hidden UI.

**Commits:** Three.
1. `feat: verdict_provider computes per-industry-group peer medians (FB-1 display half)`
2. `feat: verdict card shows peer median column beside company value`
3. `feat: verdict card shows 'As of' timestamp chip (FB-4)`

**Verification:**
- Manual: NVDA verdict card → Revenue YoY 65.5% beside Semiconductors peer median. Reveals Leader status clearly.
- Manual: GOOGL verdict card → industry_group "Telecom & Media," industry "Interactive Media," peer median from 34-ticker Telecom & Media pool. Expect GOOGL to show high positive deviation on margins — accurate, and the industry label explains why.
- Manual: timestamp chip shows `YYYY-MM-DD` matching `screener_results.csv` mtime.
- All tests pass (50 + 4 new = 54).
- Card shape identical across Ticker Lookup, Daily Report, Leader Detector.

**Team:** Two agents.
- **skipper** for `verdict_provider.py` peer computation + caching.
- **sophia** for card rendering consolidation across three tabs.

---

## Sequencing and merge strategy

**Order:**
1. Round 7a — inline expansion (low risk, high visibility).
2. Round 7b — Daily Report polish (low risk, quick wins).
3. Round 7c — classifier (data infrastructure).
4. Round 7d — peer benchmarking + timestamp (consumes 7c).

**Branching:** Each round on its own branch (`agent-round7a`, `agent-round7b`, `agent-round7c`, `agent-round7d`). Merge each to `main` after verification, before starting the next.

**CHANGELOG:** One entry per round, ideally in the round's doc pass. Catch up in the next round if skipped.

---

## Iteration-wide verification

After all four rounds merge:

- [ ] All tests pass (target: ~54 tests).
- [ ] Daily Report banner shows per-date breakdown correctly.
- [ ] Daily Report "As of" column gone.
- [ ] Strategy Lab defaults to Daily Report symbols.
- [ ] Click row on Daily Report → verdict card expands inline.
- [ ] Leader Detector has both Sector and Industry Group filter chips.
- [ ] Verdict card shows peer median column.
- [ ] Verdict card shows timestamp chip.
- [ ] Card shape identical on Ticker Lookup, Daily Report, Leader Detector.
- [ ] NVDA: big SVR card and verdict card SVR consistent OR clearly timestamped (item 3 resolution).
- [ ] GOOGL: industry_group = "Telecom & Media," industry = "Interactive Media," peer medians from 34-ticker pool.

---

## Workflow notes (from Rounds 1-6)

- **Reference PATTERNS.md P-1 in UI-touching prompts.** `[hidden]` CSS override has bitten twice.
- **Sandbox test runs are advisory, not authoritative.** User runs `pytest` locally to confirm.
- **Test-first for data-layer changes.** 7c classifier ships with tests in the same commit.
- **Atomic commits per feature.** Revertable.
- **Manual UI verification mandatory** for 7a, 7b, 7d. 7c still warrants filter-chip click-through.

---

## File references

- `NEXT_ROUNDS.md` — long-range backlog
- `FEATURE_BACKLOG.md` — FB-1 through FB-8
- `PATTERNS.md` — engineering patterns (P-1 for UI)
- `audit-findings.md` — Round 1 frozen record
- `round*-summary.md` — historical round summaries
- `CHANGELOG.md` — release notes
- `classifier.py` — (new in 7c) classification logic
- `verdict_provider.py` — (extended in 7d) peer medians

---

Written 2026-04-24 after Round 5/6 merged and user-driven feedback. Revised to reflect 29 industry groups (Interactive Media & Services merged into Telecom & Media per user direction) and canonical ticker overrides.
