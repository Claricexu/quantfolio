# Round 7c — implementation summary

Branch: `agent-round7c`, four feature commits ahead of `4f27320` (the last Round 7b summary commit). Not pushed, not merged. Three-agent team: **wright** reviewed the classifier API design before code was written, **skipper** implemented the module + tests + pipeline integration, **sophia** reviewed the Industry Group filter UX after the commit landed.

Two feedback items closed: **FB-1 (data half)** — canonical `(sector, industry_group, industry)` derivation surfaced through the screener and verdict pipeline — and **FB-5** — Industry Group filter chips on the Leader Detector tab.

---

## What shipped

| Commit | Title |
|---|---|
| `b728de6` | feat: classifier module with SIC + ticker-override rules, 10 sectors and 29 industry groups (FB-1 data half) |
| `22228e2` | feat: fundamental_screener writes sector/industry_group/industry; verdict_provider surfaces fields |
| `4343952` | feat: Industry Group filter chips on Leader Detector tab (FB-5) |
| _(this commit)_ | docs: Round 7c — classifier module, Industry Group filter, pipeline integration |

50 tests pass (41 existing + 9 new classifier tests). Tree clean. No push, no merge — gating before merge to `main` is the owner's UI verification step (see "Verification status" below).

---

## Phase 1 — wright's API design review

Skipper drafted a one-page design doc covering the `classify(symbol, sic, sic_description)` signature, `TICKER_OVERRIDES` dict, SIC rule-table structure, the 10 sectors / 29 industry groups, unknown handling, and a sample test. Wright BLOCKed with six items (1, 2, 4, 5 confirmed; 3 and 6 conflicted with the spec's hard rules and were pushed back on).

**Wright's accepted refinements (applied in Phase 2):**

1. **Bisect over tuples is a latent bug.** `bisect_right(SIC_RANGES, (sic, ...))` would compare the full tuple lexicographically — payload strings could reorder rows on ties. Switched to a parallel `_LO_KEYS: list[int]` array; bisect works on a scalar key, then a separate `lo <= sic <= hi` range-check filters out gaps between adjacent ranges.
2. **Module-load invariant.** Misordered or overlapping SIC ranges now `raise ValueError` at import time, not silently at query time. A future PR that adds an overlapping row breaks `import classifier` immediately, which cascades through every test and pipeline run.

**Pushbacks the spec required and wright accepted:**

3. **Drop `sic_description` from v1 signature** — REJECTED. The user's round prompt explicitly fixed the signature as `classify(symbol, sic, sic_description)`. Kept the parameter, documented it as "accepted, currently unused; reserved for future tie-breakers (e.g., disambiguating SIC 6199 'Finance Services' via description keywords)."
6. **Add a 10th unit test for the SIC ordering invariant** — REJECTED. The user's round prompt explicitly says "Do not write speculative tests beyond the 9 specified." The import-time invariant raise covers the same regression target — a mis-ordered PR fails `import classifier`, which collapses all 9 tests with ImportError, strictly stronger than catching the regression in a single unit test that runs after import.

Wright re-reviewed the revised design and LGTM'd. No second cycle needed.

---

## Phase 2 — `classifier.py` and 9 unit tests (commit `b728de6`)

**Module shape.** `classifier.py` is a leaf module — only `from bisect import bisect_right` from the standard library. `TICKER_OVERRIDES` is a 9-entry dict (GOOGL/GOOG/META/NFLX/AMZN/AAPL/TSLA/V/MA) matching the spec verbatim. `SIC_RANGES` is a 56-row sorted, disjoint list of `(lo, hi, (sector, industry_group, industry))` tuples covering SIC 0100-8999. The 10 sectors and 29 industry groups are all expressible:

- 10 sectors: Communication Services, Consumer Discretionary, Consumer Staples, Energy, Financials, Healthcare, Industrials, Materials, Technology, Utilities.
- 29 industry groups: per `ITERATION_PLAN_V2.md` line 160, with one rendering note below.

**Naming reconciliation — "Oil/Gas/Coal E&P/Services" vs "Oil, Gas & Coal E&P".** The spec lists industry group #16 as `Oil/Gas/Coal E&P/Services` (slashes joining four sub-categories), but test 6 (`test_classify_by_sic_oil_gas_ep`) expects `SIC 1311 → Energy / Oil, Gas & Coal E&P / Services` — three tier-separated by ` / `. The two forms can't both be the literal industry_group string. Treated the test as the explicit return-value contract: `industry_group = "Oil, Gas & Coal E&P"`, `industry = "Services"` for SIC 1311. The spec's slash-joined form is the user's shorthand for the conceptual category. The chip filter on Leader Detector renders `Oil, Gas & Coal E&P` as the chip label.

**Edge cases.** `_coerce_sic` handles int from XBRL, str from CSV, padded `"01311"`, `"1311.0"` from a stray pandas/yfinance round-trip, `None`, empty string, and non-numeric strings — all collapse to either an int or `None`. `None`/non-numeric returns `("Unknown", "Unknown", "Unknown")`. Numeric SIC that doesn't match any range returns `("Unknown", "Unknown", f"SIC {sic_int}")` per spec.

**Tests.** All 9 cases land in `tests/unit/test_classifier.py`, plain-assert style matching the rest of `tests/unit/`. Wired into `tests/unit/run_all.py` as a 9th test module. `python tests/unit/run_all.py` reports 0 failures across all 50 tests (41 prior + 9 new).

---

## Phase 3 — pipeline integration (commit `22228e2`)

**`fundamental_screener.py`.**

- New `from classifier import classify` at top of file.
- `score_ticker` calls `classify(m['symbol'], m['sic'], m['sector'])` near the top, before the rubric work, and overwrites `m['sector']`, sets `m['industry_group']`, `m['industry']`. The third arg (`m['sector']`) holds the SIC description string from `fundamental_metrics.compute_metrics` — passed as `sic_description` for forward-compat with future tie-breakers; classifier doesn't use it in v1.
- `CSV_OUT_FIELDS` grows from 30 to 32 columns: `industry_group` and `industry` appended at the right, after `dealbreakers_json`.

**Existing `sector` column collision — handled in place.** The CSV already had a `sector` column populated with `info.get('sic_description')` (a SIC description string like "Crude Petroleum and Natural Gas"). The spec said "add three new columns: sector, industry_group, industry" with "existing columns unchanged in name and order" — these are mutually inconsistent under a strict reading because a CSV cannot have two columns with the same name. Resolved by:

- **Existing `sector` column position and name unchanged.** No reorder, no rename.
- **Existing `sector` column data is now the canonical classifier sector** (e.g. "Energy") instead of the raw SIC description. This is a data quality fix — the column was misnamed before (it held a SIC description, not a sector). Frontend's `broadSector(sic, fallback)` uses `r.sector` only as a graceful fallback when SIC parsing fails; substituting a canonical sector for a SIC description there is strictly an improvement.
- **Two new columns appended:** `industry_group`, `industry`. Net change: +2 columns, +0 renames, +0 reorders.

This trade-off is documented in the Phase 3 commit's `CSV_OUT_FIELDS` block comment so future readers see the rationale. No downstream callers were broken because the `sector` column has been a free-form string in both forms.

**`verdict_provider.py`.** No code change — `_coerce_row` is shape-agnostic for unknown columns (only `_FLOAT_COLS`, `_INT_COLS`, `_BOOL_COLS` get coerced). The new `industry_group` and `industry` columns flow through to `load_verdict_for_symbol` as plain strings, surfaced in the verdict dict alongside `sector`. Module docstring updated to document the passthrough so the next maintainer doesn't think it's missing.

**Tests after Phase 3.** Imports succeed (`python -c "import fundamental_screener"`). `score_ticker` smoke test on a synthetic AAPL metrics dict returns canonical `(Technology, Hardware & Equipment, Tech Hardware & Networking)` via the ticker override. `CSV_OUT_FIELDS` has 32 entries, last two are `industry_group`, `industry`. No tests in the `tests/` tree import `fundamental_screener` or `verdict_provider`, so the existing 50-test suite is untouched.

---

## Phase 4 — Industry Group filter chips on Leader Detector (commit `4343952`)

**Structural mismatch with the spec — flagged and resolved without refactor.** The spec said "add Industry Group filter chip row immediately below the existing Sector filter chip row." The existing Sector filter on Leader Detector is implemented as a `<select>` dropdown (`#leadersSectorSelect`), not a chip row. Two interpretations:

- _Refactor Sector to chips first, then add Industry Group chips beneath._ Out of scope for Round 7c, would expand the diff substantially, and the user's process notes say "If skipper hits a structural mismatch (e.g., the Sector filter chip code is so different from what's needed for Industry Group that it requires a refactor), STOP and report."
- _Add Industry Group chips below the existing controls block, leveraging the existing chip pipeline (`.ldr-chip` + `onLeadersFilterChip`) without touching Sector._ Closest correct interpretation of the spec; no refactor; placement is "below" as the spec asked, just below the entire controls block (which contains the Sector dropdown) rather than below an imaginary Sector chip row.

Skipper went with the second option since it doesn't require a refactor proposal to wright. The Sector filter staying as a dropdown is an _existing_ inconsistency the spec author may not have known about; harmonizing it with chips is a future round's call. Documented here as a structural note for the owner's verification.

**Implementation.**

- New filter-state field: `_leadersFilter.industry_group` defaulting to `'ALL'`. `applyLeadersFilters` ANDs five filters now: sel + verdict + archetype + sector + industry_group.
- New container row `#leadersIndustryGroupRow` in the controls block with a thin `border-top` divider and a `INDUSTRY GROUP` label matching the existing `font-size:10px;letter-spacing:1px;color:var(--text-muted)` family. Inside it, `#leadersIndustryGroupChips` is the dynamically-populated flex container.
- New `populateIndustryGroupChips()` function — mirrors `populateSectorDropdown`'s "count within OTHER active filters" pattern. The chip pool excludes the industry_group filter itself but DOES respect sel + verdict + archetype + sector, so Sector=Technology + Industry Group=Semiconductors narrows correctly. Falls back to `ALL` if the active industry_group has zero matches in the new pool.
- Cross-narrowing: `populateSectorDropdown` now also filters by industry_group (so picking Industry Group=Semiconductors narrows the Sector dropdown's options correctly). Both repopulators are called from `onLeadersFilterChip` and `onLeadersSectorChange` so any filter change re-syncs the others.
- Chips reuse the existing `.ldr-chip` and `.ldr-chip-n` classes verbatim. Click routes through the existing `onLeadersFilterChip(this)` handler via `data-filter="industry_group"` — no new dispatcher.
- `resetLeadersFilters` includes `industry_group: 'ALL'` and re-runs both repopulators.

**P-1 (CSS-vs-hidden):** the new chip-row container does NOT use the HTML `hidden` attribute; visibility is governed by the parent `#leadersControls` `style.display` toggle in `loadLeaders`. P-1 only applies when an element uses both `hidden` AND a CSS `display` rule. N/A here.

**P-2 (no synchronous layout flushes):** the only DOM mutation introduced is `wrap.innerHTML = chips.join('')` on `#leadersIndustryGroupChips` — a small flex container (≤30 chip buttons), separate from the 1,414-row leader table. No `focus()`, no `scrollIntoView()`, no `getBoundingClientRect()`, no reads of layout properties anywhere in the new code path. The downstream `renderLeadersTable` was already in place and uses the same single-`innerHTML`-replace pattern that has been the convention since Round 7a.

**P-4 (caller-chain trace):**
- _Sector filter chip rendering caller chain:_ `loadLeaders → populateSectorDropdown → <select>` options. Now also: `loadLeaders → populateIndustryGroupChips → <button>` chips.
- _Sector click handler chain:_ `<select onchange> → onLeadersSectorChange → populateIndustryGroupChips → renderLeadersTable`.
- _Other-chip click handler chain:_ `<button onclick> → onLeadersFilterChip → populateSectorDropdown + populateIndustryGroupChips → renderLeadersTable`.
- _Industry Group click handler chain:_ same `onLeadersFilterChip` (data-filter routes correctly), so cross-narrowing happens automatically.

**All-surfaces check — Round 7a inline expansion.** The row click handler `onclick="openSymbolDetail(event, '${escapeHTML(sym)}')"` is set in the row template inside `renderLeadersTable` (line 3266). When the table re-renders for a filtered view, every visible row still carries the same click handler. `openSymbolDetail` uses `evt.currentTarget.closest('tr')` and dispatches by `closest('#libTable')` — neither cares about row position or filter state. Verified by code-reading; the inline-verdict-card expansion still works in any filtered view.

---

## Sophia's UX review (Phase 4 post-commit)

**Verdict: LGTM with three non-blocking follow-ups.**

- **Visual integration** — acceptable. The `border-top` divider and matching label style tie the Industry Group row to the same filter family as SHOW/VERDICT/ARCHETYPE/SECTOR. Reads as a second tier rather than a peer (correct — it _is_ downstream of Sector).
- **Cross-narrowing** — symmetric and correct in both directions. `populateIndustryGroupChips` filters by sector before counting; `populateSectorDropdown` filters by industry_group before counting; `applyLeadersFilters` ANDs all five.
- **Chip-styling consistency** — count format `(N)` matches `cntSelAll/cntSelSelected` exactly. Asymmetry between Verdict/Archetype (no counts) and SHOW + Industry Group (counts) was pre-existing.
- **Round 7a inline-expansion preservation** — safe. `renderLeadersTable` rebuilds row HTML each call; `onclick="openSymbolDetail(...)"` is in the template literal.
- **Edge cases** — stale-active-chip fallback handled, 29-chip wrap is OK on desktop, empty `industry_group` rows skipped from chip generation but still visible under "All".
- **Naming** — "INDUSTRY GROUP" label is right; mislabeling it "Industry" would silently mislead about peer-median benchmarking semantics.

---

## Deferred items (sophia's non-blocking follow-ups)

These are forward-looking improvements, not Round 7c blockers:

1. **`aria-pressed` on chips** — pre-existing accessibility gap across all `.ldr-chip` instances (Verdict, Archetype, SHOW, and now Industry Group). Screen-reader users hear "All button, Leader button…" with no indication of which is active. Future round: add `aria-pressed` toggling and `role="group"` wrappers with `aria-label` per chip family.
2. **Mobile chip-row height** — with up to 29 industry-group chips visible at once, the row consumes vertical real estate on small screens. Future round: collapse to "Show more" beyond N=12, or move Industry Group to a dropdown on `< 640px`.
3. **Visual count parity across chip families** — Verdict and Archetype chips have no count badges; SHOW and Industry Group do. Future round: add counts to Verdict and Archetype for internal consistency.

These are tracked here for `NEXT_ROUNDS.md` /  Round 7d backlog discussion.

---

## Verification status

- **Tests:** `python tests/unit/run_all.py` → 50 tests, 0 failures (41 existing + 9 new in `test_classifier`).
- **DEVELOPMENT.md:** §2 repo map now lists `classifier.py` with a one-line description; §3 architecture summary now has a bullet on the classification pipeline with `TICKER_OVERRIDES` rationale.
- **Tree:** clean after this docs commit. No push, no merge.
- **Owner manual UI step (required before merge to `main`):**
  - Re-run `python fundamental_screener.py --universe universe_prescreened.csv --csv-out screener_results.csv` to regenerate `screener_results.csv` with the canonical `sector` column and the new `industry_group` / `industry` columns. (Without this, the Leader Detector chip filter sees no data — the existing CSV predates Phase 3.)
  - Hard-refresh `localhost`, open Leader Detector tab, confirm:
    - Industry Group chip row renders below the existing controls block, with a thin divider above it.
    - Selecting Sector=Technology narrows Industry Group chip counts to Technology rows; selecting Industry Group=Semiconductors then further narrows the table.
    - Picking Industry Group=Semiconductors re-narrows the Sector dropdown to options that contain Semiconductors rows.
    - Clicking a row in the filtered view still expands the inline verdict card from Round 7a.
    - "Clear filters" resets both Sector and Industry Group to ALL.
  - Search for GOOGL in screener data, confirm `sector = "Communication Services"`, `industry_group = "Telecom & Media"`, `industry = "Interactive Media"`.

---

## Files touched

- `classifier.py` (new, 250 lines including SIC range table)
- `tests/unit/test_classifier.py` (new, 9 tests)
- `tests/unit/run_all.py` (+1 line + 1 import)
- `fundamental_screener.py` (+22 lines: import + score_ticker hook + 2 CSV columns)
- `verdict_provider.py` (+8 lines: docstring update only)
- `frontend/index.html` (+105 / -9 lines: state field + HTML row + 2 JS functions + 4 handler hooks)
- `DEVELOPMENT.md` (+2 lines: §2 repo map + §3 architecture bullet)
- `round7c-summary.md` (this file)
