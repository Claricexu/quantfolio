# Round 7c — implementation summary

Branch: `agent-round7c`, six feature/fix commits ahead of `4f27320` (the last Round 7b summary commit) plus this docs update. Not pushed, not merged. Three-agent team: **wright** reviewed the classifier API design before code was written and re-reviewed the verification-round-8 trace before the bug fixes, **skipper** implemented the module + tests + pipeline integration + the verification-round-8 fixes, **sophia** reviewed the Industry Group filter UX after Phase 4 landed and the verdict-card / compare-card UX after the fixes landed.

Two feedback items closed: **FB-1 (data half)** — canonical `(sector, industry_group, industry)` derivation surfaced through the screener and verdict pipeline — and **FB-5** — Industry Group filter chips on the Leader Detector tab.

---

## What shipped

| Commit | Title |
|---|---|
| `b728de6` | feat: classifier module with SIC + ticker-override rules, 10 sectors and 29 industry groups (FB-1 data half) |
| `22228e2` | feat: fundamental_screener writes sector/industry_group/industry; verdict_provider surfaces fields |
| `4343952` | feat: Industry Group filter chips on Leader Detector tab (FB-5) |
| `7fd7f7e` | docs: Round 7c — classifier module, Industry Group filter, pipeline integration |
| `0cc7889` | fix: api_server injects classifier sector/industry_group/industry into /api/predict[-compare] so Ticker Lookup matches Leader Detector |
| `75b4988` | fix: frontend reads canonical v.sector/r.sector instead of broadSector for verdict card and Leader Detector; add Industry Group + Industry rows |
| _(this commit)_ | docs: round 8 verification — bug, diagnosis, fix narrative |

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

## Files touched (Phases 1-5)

- `classifier.py` (new, 250 lines including SIC range table)
- `tests/unit/test_classifier.py` (new, 9 tests)
- `tests/unit/run_all.py` (+1 line + 1 import)
- `fundamental_screener.py` (+22 lines: import + score_ticker hook + 2 CSV columns)
- `verdict_provider.py` (+8 lines: docstring update only)
- `frontend/index.html` (+105 / -9 lines: state field + HTML row + 2 JS functions + 4 handler hooks)
- `DEVELOPMENT.md` (+2 lines: §2 repo map + §3 architecture bullet)
- `round7c-summary.md` (initial version + this round 8 update)

---

## Verification round 8 — bug found in owner spot-check, fixed

### The bug — owner spot-checked 9 tickers across both tabs

After the initial Round 7c shipped, the owner ran a 9-ticker spot check comparing the Sector / Industry Group / Industry values shown on Ticker Lookup vs Leader Detector. The expected canonical table:

| Ticker | Sector | Industry Group | Industry |
|---|---|---|---|
| GOOGL | Communication Services | Telecom & Media | Interactive Media |
| META | Communication Services | Telecom & Media | Interactive Media |
| MSFT | Technology | Software & IT Services | Software & IT Services |
| AAPL | Technology | Hardware & Equipment | Tech Hardware & Networking |
| NFLX | Communication Services | Telecom & Media | Interactive Media |
| TSLA | Consumer Discretionary | Autos & Components | Automobiles & Components |
| V | Financials | Capital Markets | Payments |
| AMZN | Consumer Discretionary | Retail & Restaurants | Retail |
| MA | Financials | Capital Markets | Payments |

Only MSFT matched across both tabs. For all others, Ticker Lookup showed Yahoo Finance categories (e.g. GOOGL: "Communication Services / Internet Content & Information") while Leader Detector showed JS-side SIC-derived categories (e.g. GOOGL: "Technology"). Neither matched the classifier's canonical override values.

### Diagnosis — two display bugs, single shared root cause

**Trace 1 — Ticker Lookup compare card.** `runPredict(sym)` (`frontend/index.html:1058`) calls `/api/predict-compare/${sym}`. The handler in `api_server.py:551` routes to `predict_ticker_compare` in `finance_model_v2.py:599`, which builds a result dict from `info.get('sector')` / `info.get('industry')` (Yahoo's free-form strings — line 574 of finance_model_v2.py, propagated through lines 630-631 of predict_ticker_compare). The frontend `buildCompareCard(d)` reads `d.sector` and `d.industry` directly. /api/predict-compare bypasses verdict_provider entirely, so classifier output never reaches this card.

**Trace 2 — Ticker Lookup verdict card.** `runPredict` separately fetches `/api/screener/${sym}` (`frontend/index.html:1073`) which goes through `verdict_provider.load_verdict_for_symbol`. After Phase 3, `v.sector`, `v.industry_group`, `v.industry` ARE the canonical classifier values. But `buildVerdictCard(v)` line 995 reads `broadSector(v.sic, v.sector)` — a JS-side SIC-to-broad-sector map (lines 844-867) that ignores the second arg whenever a SIC parses. For GOOGL (SIC=7370), broadSector returns "Technology" (line 853 maps 7370-7379 to "Technology") regardless of `v.sector` being "Communication Services". The verdict card never read `v.industry_group` or `v.industry` at all.

**Trace 3 — Leader Detector SECTOR column.** `_rowSector(r)` (`frontend/index.html:2985`) calls the same `broadSector(r.sic, ...)`. Even though Phase 3 made `r.sector` canonical in the CSV, the frontend ignored it via broadSector. Same SIC=7370 → "Technology" wrong-answer story for GOOGL/META/NFLX.

**Shared root cause.** Two faces of the same gap: Phase 3 made the backend canonical via `verdict_provider`, but (a) the `/api/predict[-compare]` path never read `verdict_provider` so Yahoo's values flowed straight through, and (b) the frontend's three classifier-display surfaces (verdict card row 995, Leader Detector `_rowSector`, compare card sector box) all bypassed the canonical fields — broadSector for the screener-fed paths, direct Yahoo passthrough for the predict-fed path.

Wright reviewed this trace before any code was written and LGTM'd: "broadSector is the SHARED root cause for verdict card row 995 and Leader Detector `_rowSector`; /api/predict-compare bypassing verdict_provider is the SEPARATE cause for the compare card." Wright recommended split commits (backend + frontend) for independent revertability.

### The fix — two commits

**Backend — commit `0cc7889`** (`api_server.py`, +47 lines):

- New helper `_inject_classifier_fields(result, symbol)` looks up SIC from `verdict_provider.load_screener_index()` (cheap — mtime-keyed cache, no per-request CSV re-read), calls `classifier.classify(symbol, sic, result.get("industry"))`, and overlays `result["sector"]`, `result["industry_group"]`, `result["industry"]` IF the classifier returns a non-Unknown sector. ETFs and off-list stocks (classifier returns Unknown) keep Yahoo's `sector`/`industry` and have no `industry_group` field (frontend renders em-dash). Wright's call: "'Unknown' on the verdict card for an ETF is a worse UX regression than showing Yahoo's slightly-different taxonomy."
- Three call sites injected: `/api/predict` (line 466), `/api/predict-compare` cached path (line 569), `/api/predict-compare` fresh path (line 587).
- TICKER_OVERRIDES wins regardless of SIC because it's keyed on symbol, so GOOGL/META/NFLX/AMZN/AAPL/TSLA/V/MA always classify correctly even before the screener CSV regenerates.
- Constraint check: zero modifications to `finance_model_v2.py`, `backtest_engine.py`, or `http_client.py`. Post-processing happens strictly in the API layer.

**Frontend — commit `75b4988`** (`frontend/index.html`, +32 / -4 lines):

- `buildVerdictCard` (line ~995): replaced `broadSector(v.sic, v.sector)` with `v.sector || '—'`. Added two new rows directly below: `['Industry Group', v.industry_group || '—']` and `['Industry', v.industry || '—']`. Verdict card now shows three classifier-derived rows in 1:1 correspondence with the spot-check table's three columns.
- `_rowSector(r)` (line ~2985): prefer `r.sector` directly when non-empty; fall back to `broadSector(r.sic, ...)` only for legacy CSV rows missing the column. The fallback path is preserved so a CSV that predates Phase 3's column write still renders something rather than blank cells.
- `buildCompareCard` (lines ~1118-1162): backend already overlays canonical fields onto `d.sector` / `d.industry`, so the existing reads now pull the canonical values. Added a small extra line beneath the existing classDetail line: `Industry Group: ${classGroup}`, font-size 10px, color text-faint, only rendered when `classGroup` is present (Yahoo fallback path doesn't have one, so the line is suppressed there). Sophia's label-clarity tweak: matches the chip filter's "INDUSTRY GROUP" label exactly so vocabulary stays consistent across surfaces.

### Sophia's UX review (post-fix)

LGTM with two suggestions, one applied (the "Industry Group:" label tweak above) and one filed for Round 7d:

- **Verdict card row redundancy** — for tickers where industry_group == industry (e.g. MSFT: both "Software & IT Services"), the verdict card stacks two identical-value rows. Sophia kept this as-is: "the duplication is information — it tells the user 'this company's industry IS its group; there's no finer slice.' That's truthful and matches what peer benchmarking will do." The 1:1 map between verdict-card rows and the spot-check table also wins over a collapsed inline-pair format.
- **Round 7d layout flag** (forwarded to wright for next round): the verdict card's existing `display:flex; justify-content:space-between` two-child row layout (line 1006) should refactor to a 3-column grid before the peer-median column lands, so Sector and Industry rows can render an em-dash in the peer column without collapsing alignment. Sophia: "Worth a heads-up to wright now, not a block on this commit."

### Verification round 8 — owner spot check after this docs commit

Each of the 9 tickers should show identical Sector / Industry Group / Industry in:

1. **Ticker Lookup verdict card** — three consecutive rows in the rows[] table near the bottom of the card (look for "Sector", "Industry Group", "Industry").
2. **Ticker Lookup Sector box** (top-right of the compare card) — primary line is Sector, secondary line is Industry, third small line is "Industry Group: X".
3. **Leader Detector SECTOR column** — main table.
4. **Inline verdict from Leader Detector click** — row click expands the same verdict card from (1) inline.

Owner steps:

1. Run `python fundamental_screener.py --universe universe_prescreened.csv --csv-out screener_results.csv` to regenerate the screener CSV with classifier-canonical sector + new industry_group / industry columns. (Without this, only the override tickers show canonical values via the API-layer overlay; non-override SIC-mapped tickers still show whatever the old CSV held.)
2. Restart `api_server.py` so the new `_inject_classifier_fields` helper is loaded and the verdict_provider mtime cache picks up the new CSV.
3. Hard-refresh `localhost:8000` so the frontend picks up the JS changes.
4. Run the 9-ticker spot check above. Confirm GOOGL/META/NFLX show Communication Services / Telecom & Media / Interactive Media on both tabs; AMZN shows Consumer Discretionary / Retail & Restaurants / Retail; AAPL shows Technology / Hardware & Equipment / Tech Hardware & Networking; TSLA shows Consumer Discretionary / Autos & Components / Automobiles & Components; V/MA show Financials / Capital Markets / Payments; MSFT shows Technology / Software & IT Services / Software & IT Services.
5. Test the ETF fallback: search for SPY (or any ETF) on Ticker Lookup; sector should still be Yahoo's value (no Unknown), and the "Industry Group:" line should NOT appear.

### Files touched (round 8 fixes)

- `api_server.py` (+47 lines: classifier import + injection helper + 3 call sites)
- `frontend/index.html` (+33 / -4 lines: verdict card rows + _rowSector preference + compare card industry-group line + sophia's label tweak)
- `round7c-summary.md` (this update)

### Tests

All 50 tests still pass after both fix commits — none of them touch `api_server.py`, `frontend/index.html`, or any other file modified in round 8. Smoke-tested `_inject_classifier_fields` directly: GOOGL → ('Communication Services', 'Telecom & Media', 'Interactive Media'); MSFT → ('Technology', 'Software & IT Services', 'Software & IT Services'); SPY (off-list) → Yahoo values preserved, no industry_group set.
