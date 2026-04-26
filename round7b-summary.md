# Round 7b — implementation summary

Branch: `agent-round7b` (2 feature commits ahead of `b52ff16`, plus this summary). Not pushed, not merged. Frontend-only, single-agent (skipper).

Two feedback items closed: **FB-7** (Daily Report banner aggregates per-date close prices) and **FB-6** (Strategy Lab defaults to Daily Report symbols).

---

## What shipped

| Commit | Title |
|---|---|
| `3f68a94` | feat: Daily Report banner aggregates per-date close prices; remove redundant per-row column (FB-7) |
| `02d1636` | feat: Strategy Lab defaults to Daily Report symbols (FB-6) |

Both commits live in `frontend/index.html` only. No backend changes. No test changes.

---

## FB-7 — banner aggregation

**Backend field check.** `/api/report` already returns `last_date` per row (set in `finance_model_v2.py:predict_ticker` at line 579, surfaced verbatim through `predict_ticker_compare` and `daily_scan_both`). Verified directly against the most recent cached payload `data_cache/dual_report_20260424_1842.json` — every row carries `last_date` in `YYYY-MM-DD` form. No backend change needed; spec point C cleared.

**Implementation.** New `_buildAsOfBreakdown(data)` helper aggregates rows by `last_date`. Plugged into the existing `renderReportStatusBanner` so both call sites — fresh-fetch path (`pollReport` -> `renderReport`) and cached-load path (`loadReport` -> `renderReport`) — flow through the same banner. Single distinct date renders as `Close prices as of <Date>`; multiple dates render as comma-separated `<N> symbols' close price as of <Date>` clauses, no truncation, newest-first via lex sort on `YYYY-MM-DD`.

**Spec point A — per-row "As of" column removal.** The column does not exist in the current rendered DOM. `buildReportRow` emits exactly 10 `<td>` cells aligned to the 10 `cols` defined in `buildReportTable` — Symbol, Price, Lite Chg, Lite Sig, Pro Chg, Pro Sig, Consensus, Conf, Best Strategy, Firm Score. The only "As of"-style UI on the Daily Report tab is a small `as-of-chip` rendered on the **Firm Score column header** (line 1930-1932 post-commit), which reflects `screener_results.csv` mtime — verdict freshness, NOT close-price freshness. That chip is intentionally left in place because it tracks a different freshness axis from the new banner clause. Removing a never-rendered column was a no-op; documented in the commit message body so future readers don't go hunting for the diff.

**Sort logic.** `SORT_KEYS` (line 1864) has no `last_date` / `as_of` key, so the (non-existent) column removal has no sort-side cost.

**Verification.**
- Helper logic exercised in a Node REPL run against six fixture sets (single-date, two dates, three dates, empty array, all-rows-missing-`last_date`, partial-date with null row). Output matches spec.
- All-surfaces check: `renderReportStatusBanner` is the single banner-render entry point; both fresh-fetch and cached-load paths reach it via `renderReport`. Both branches in the helper (parseable / unparseable timestamp) preserved; only the parseable branch grew the new tail clause.
- JS syntax-checked via `node --check` on the extracted `<script>` block.

---

## FB-6 — Strategy Lab default filter

**Daily Report symbol source.** New module-level `_dailyReportSymbols` (Set, lazy-init). Two populators:

1. `renderReport` calls `_setDailyReportSymbols(rawData)` before the SELL-downgrade transform, so opening or refreshing the Daily Report tab keeps the set fresh.
2. `_ensureDailyReportSymbols()` (called from `loadLibrary` in parallel with `/api/backtest-library`) pulls cached `/api/report` so the filter works even when the user opens Strategy Lab BEFORE Daily Report this session. The backend's existing `_load_latest_report_from_disk` cold-start fallback (resolved by Round 7a's `3d1fbae`) means a fresh server + a cached `dual_report_*.json` on disk is enough — no new fetch is triggered.

**Render path.** Single `_renderLibraryWithFilter()` is the source of truth for "what does Strategy Lab look like right now" — initial load, toggle change, and post-batch rebuild all reach the same place. It builds a cloned payload with `data: filteredItems` and feeds it to the existing `renderLibraryCallout` + `renderLibraryTable`, so the recommendations callout, sort state, and chart-on-row-click path all operate on the filtered set without further changes.

**Cache split.** Introduced `_libDataAll` (raw API payload) alongside the existing `_libData` (post-filter, currently displayed). Post-batch reload now nulls both so the next `loadLibrary` refetches and re-filters cleanly.

**Edge case (no overlap).** If Daily Report has symbols but none of them have a cached backtest, render an honest skeleton (rather than an empty table) pointing at the toggle as the escape hatch. Picked this over silently showing zero rows because zero rows look like a bug. Documented in the commit message.

**Verification (code-trace).**
- Filter gates: toggle off + Daily Report present -> filter; toggle off + Daily Report absent -> show all + notice; toggle on -> show all + no notice (notice tracks data-source absence, not active mode).
- Notice visibility: only when `_dailyReportSymbols.size === 0` AND a library is loaded.
- Sort: operates on `_libItems` which is set from filtered `json.data` -> sort respects filter.
- Round 7a inline strategy-chart expansion: `openSymbolDetail` keys off `#libTable` ancestry, unaffected by which rows are in tbody. `onLibShowAllChange` calls `closeDetail()` before re-render so an open chart doesn't drift to a stale tbody node.
- Post-batch rebuild: `_libData = null` + new `_libDataAll = null` ensures `loadLibrary` refetches.
- JS syntax-checked via `node --check`.

---

## DOM/CSS decisions

### Notice placement and styling
- `<div id="libNoReportNotice" class="app-banner" role="status" hidden>` placed immediately under the Strategy Lab header div, above `#batchStatus` and `#libCallout`. Reuses the existing `.app-banner` family (line 313); no new CSS class introduced.
- P-1 compliance: `.app-banner[hidden] { display:none; }` companion rule already exists at line 323 — toggling `notice.hidden` works correctly. Inline comment in markup references P-1.

### Override toggle styling
- `<input type="checkbox" id="libShowAll">` with `<label for="libShowAll">Show all symbols</label>` rendered in a new `#libFilterRow` flex row directly under the notice. Uses `accent-color: var(--blue-light)` to match the existing blue accent palette. No new CSS class — inline `style` attributes only, matching the inline-style pattern already used in adjacent header markup.
- Right side of the row carries `#libFilterStatus` — small text-faint line that reports the current filter state ("Filtered to N of M symbols (current Daily Report)" / "Showing all M cached symbols (no Daily Report data)" / "Showing all M cached symbols").
- Toggle row hides itself entirely when the library is empty (`rawItems.length === 0`) — the H-11 skeleton already explains the state and a stray checkbox would be confusing.

### Banner format details (FB-7)
- Single-date clause: literal `Close prices as of YYYY-MM-DD` (no count).
- Multi-date clause: `<N> symbols' close price as of YYYY-MM-DD` per group, comma-separated.
- Newest date appears first (lex-descending sort on `YYYY-MM-DD`).
- Append uses a leading ` | ` separator only when the clause is non-empty, so a payload with all-missing `last_date` falls back to the original two-clause banner without a stray pipe.

---

## Deferred items / known limitations

1. **Filter status copy is informational, not interactive.** The `Filtered to N of M symbols` line near the toggle is read-only text. A future polish round could make the count clickable to reveal which symbols are excluded.
2. **No keyboard shortcut for the toggle.** The checkbox is reachable via Tab from the surrounding controls but has no dedicated accelerator (e.g. `Alt+S`). Defer until/unless owner asks for it.
3. **`_dailyReportSymbols` doesn't refresh inside Strategy Lab without a tab-switch.** If the user rebuilds the Daily Report in another window/tab while Strategy Lab is open, the filter shows stale symbols until they navigate away and back. Acceptable: the daily-scan cadence is once per weekday, not minute-by-minute.
4. **`_ensureDailyReportSymbols` swallows fetch errors silently.** A failed `/api/report` call logs nothing; the filter falls back to "show all + notice." Defensible (the notice surfaces the missing-data state to the user) but a deferred polish item could add a console.warn.
5. **No automated tests added.** Per round constraint ("Do NOT touch tests"). The behaviour is pure DOM + small data-flow plumbing; the test surface (notice visibility, filter set membership, toggle state machine) is testable but the project's frontend test harness isn't present yet. Filing as a follow-up for whichever round adds frontend test infra.
6. **No-overlap edge case skeleton copy uses `_dailyReportSymbols.size`.** That count is correct (all DR symbols, not just unique-vs-library) but might mislead users who don't realize that "all 174 symbols" includes ones that haven't been backtested. Acceptable for the rare edge case; revisit if it confuses owners.

---

## Verification status

| Check | Result | Notes |
|---|---|---|
| Code-read trace | DONE | Banner helper, filter helper, render path, post-batch reload path, notice visibility, openSymbolDetail compatibility all walked. |
| `[hidden]` CSS pair (P-1) | VERIFIED | `.app-banner[hidden]` rule exists at line 323; new notice uses `class="app-banner"` and toggles `el.hidden`. |
| P-2 layout-flush | NOT APPLICABLE for new code | Toggle re-render is a `tbody.innerHTML` rewrite, identical shape to the existing sort path; Strategy Lab table is small (~few hundred rows). |
| JS syntax | VERIFIED | `node --check` on extracted script block. |
| Banner aggregation logic | VERIFIED | Six-case Node REPL run; matches spec for single/multi/three/empty/no-date/null-row inputs. |
| Backend test suite | PASS | `tests/unit/run_all.py` reports 0 failures (41 tests) — pre-Round-7a baseline preserved. Run captured at session end below. |
| **Browser click-through** | **DEFERRED to owner** | Per Round 7a precedent. See the Verification round 7 cases below. |

### Verification round 7 (owner click-through)

Hard-refresh after each commit before testing the next.

**FB-7 banner:**
1. Open Daily Report with the existing cached report. Confirm the banner reads `Report generated: ... | <X> symbols analyzed | Close prices as of 2026-04-24` (single date).
2. Force a multi-date scenario by clicking **Refresh Report** while one ticker is offline / partial-fetch. Confirm the banner shows the comma-separated `<N> symbols' close price as of <Date>` form.
3. Confirm the banner appears identically when reloading the page (cached-load path) and when refreshing the report (fresh-fetch path).

**FB-6 filter:**
1. Open Strategy Lab with a current Daily Report cached: only DR symbols visible; status reads `Filtered to N of M symbols (current Daily Report)`; notice hidden.
2. Restart the server, open Strategy Lab BEFORE Daily Report: filter still applies if `dual_report_*.json` exists on disk (cold-start fallback path).
3. Delete or move all `dual_report_*.json` files, restart, open Strategy Lab: notice visible at top, status reads `Showing all M cached symbols (no Daily Report data)`, toggle defaults to off.
4. Check **Show all symbols**: filter bypasses, status reads `Showing all M cached symbols`, notice does NOT appear (notice tracks data absence, not toggle).
5. Uncheck: filter re-engages, prior status returns.
6. Page reload: toggle resets to off (default filtered view) regardless of pre-reload state.
7. Click a row in the filtered library: Round 7a inline strategy chart expands beneath that row.

---

## Test run (session end)

Test output captured at the end of the session — pre-Round-7a baseline of 41 tests preserved with 0 failures.

```
======== TOTAL FAILURES: 0 ========
```

Full output: `tests/unit/run_all.py` ran cleanly via `C:\Users\xkxuq\miniconda3\envs\fin\python.exe`. All eight test files pass: `test_config_hash`, `test_backtest_engine_edge_cases`, `test_backtest_engine_basic`, `test_api_backtest_wire_format`, `test_http_client`, `test_edgar_fetcher_http`, `test_yfinance_http`, `test_predict_ticker_warnings`.

---

## Session end

Two features shipped, two commits on `agent-round7b`. Branch is ready for owner verification round 7, then merge to `main`. No backend changes; no test changes; Round 7a inline-expansion behavior preserved on Daily Report verdict card AND Strategy Lab strategy chart.
