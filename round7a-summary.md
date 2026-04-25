# Quantfolio round 7a — implementation summary

Branch: `agent-round7a` (2 feature commits ahead of `main`, plus this summary).
Not pushed, not merged. Review / merge decision is yours.

Three-agent team (skipper writes, wright + sophia review). Two feedback items closed: **FB-2** (verdict card inline expansion on Daily Report + Leader Detector) and **FB-8** (strategy comparison chart inline expansion on Strategy Lab library). Three review passes — Wright BLOCKed v1 with 7 concerns, LGTM'd v2 after all were addressed; Sophia LGTM'd both phases with non-blocking notes deferred to Phase 7d.

---

## ASK — backend follow-up authorization needed

Round 7a closes with one outstanding owner-reported regression that this round CANNOT fix because the root cause lives in `api_server.py`, not in `frontend/index.html`. The user reported a ~50-second recompute delay on the Ticker Lookup tab; investigation traced it to two server-side issues (full diagnosis in **Regression B investigation** below). The fix requires two backend changes:

1. **`api_server.py:488`** — widen or remove the 22-hour cache-freshness gate that rejects the most recent dual report on disk (`dual_report_20260424_1842.json`, ~22h 48m old at investigation time, was rejected).
2. **`api_server.py:478-482`** — extend `_get_cached_compare_result` to call `_load_latest_report_from_disk()` when `_report_cache["data"]` is None, so cold-start (server restart before opening Daily Report) hits the disk cache instead of falling through to a fresh 50s compute.

Round 7a is scoped frontend-only (per round constraints). **Requesting authorization for a backend follow-up round** (Round 7a-backend or Round 7b) to make these two surgical changes. No frontend prediction cache will be added — that would be the speculative caching the owner explicitly forbade, and the data is already cached on disk.

---

## What shipped

| Commit | Title | Finding |
|---|---|---|
| `2677b36` | feat: verdict card expands inline beneath clicked row on Daily Report and Leader Detector | FB-2 |
| `04842f2` | feat: strategy comparison chart expands inline beneath clicked library row | FB-8 |

Both commits live in `frontend/index.html` only — no backend changes, no test changes, per round constraints.

---

## DOM structure decisions (Phase 1)

The two features share a single dispatch path so the DOM contract stays uniform across tabs.

- **Single shared dispatcher** — `openSymbolDetail(evt, symbol)`. Tab/kind detected via `clickedRow.closest('#libTable')` (chart) vs default verdict-card path. One function owns all three tabs.
- **Detail row markup** — `<tr class="detail-row" data-detail-for="{symbol}"><td colspan="N" class="detail-cell"><div class="detail-card-host" tabindex="-1">…</div></td></tr>`. The `colspan` is read from `clickedRow.children.length` at click time so it adapts to the 10/9/8 column variance across the three tables without hard-coding.
- **Responsive switch** — below 700px viewport (existing breakpoint), the modal overlay engages instead of the inline detail-row. One detail open at a time globally.
- **Open-state tracking** — module-level `openDetail = { symbol, rowEl, detailRowEl, mode, kind, abortCtrl }`. Single source of truth.
- **Stale-DOM safety** — `sortReportTable` and `sortLibraryTable` call `closeDetail()` before `tbody.innerHTML = …`. Tab-switch also closes. Prevents references to nodes that the sort would otherwise nuke.
- **Race safety** — AbortController per fetch, aborted in `closeDetail()`. Symbol-match guards before any DOM write so a late response from a prior open does not paint into the current one.
- **P-1 compliance** — `.detail-modal-overlay` and `.detail-modal-card` both have `[hidden]` companion rules. Inline `.detail-row` / `.detail-cell` / `.detail-card-host` are dynamically created and destroyed (no `[hidden]` toggling), so P-1 does not apply to those classes.

---

## Deferred items / known limitations

Sophia's two review passes raised eleven non-blocking items. All deferred to Phase 7d (a11y + polish sweep):

1. **Inline mode has no Escape-to-close** — only modal mode binds Escape. Two-line fix candidate.
2. **Symbol cell is mouse-only** — `<td class="symbol-cell">` has `cursor:pointer` + `onclick` but no `tabindex` / `role="button"` / keyboard activation.
3. **Focus-restore is a no-op** — `closeDetail()` calls `.focus()` on the `<td class="symbol-cell">`, which does nothing without `tabindex`. Resolved by fixing #2.
4. **Modal does not focus-trap** — Tab from inside the modal escapes to the page behind.
5. **No discoverability hint above tables** — Daily Report relies on the existing `title="Click for fundamental detail"` tooltip only. Re-click-row-to-collapse is the only documented close mechanism. — **Resolved by `b118394`** (explicit × close button added to inline verdict + chart cards).
6. **Resize-while-open keeps current mode** — no auto-switch between inline and modal on viewport-cross. Documented in code comment; Wright judged auto-switch premature for 7a.
7. **Library row hint omits the 4-8 minute first-time backtest wait** — copy update.
8. **Library row `<tr onclick>` lacks ARIA** — no `role="button"` / `aria-expanded` / keyboard handler.
9. **Active-row + detail-row visual seam** — two adjacent shaded greys with a dashed border between them. Functional but busy.
10. **Library poll race window** — the 3000ms interval can fire one more time between user-close and the next tick. Harmless (symbol-match guard catches it) but not deterministic.
11. **Legacy `.detail-card-wrap` CSS rules unreferenced** — left in place to keep this round's diff focused. Cleanup sweep candidate.

---

## Patch round (post-merge user feedback)

After the initial Phase 2/3 commits landed, owner click-through surfaced three issues. All three patched on `agent-round7a` without re-merging from `main`.

| Commit | Title | Driver |
|---|---|---|
| `aec5e05` | fix: Daily Report and Leader Detector verdict card no longer clipped by parent column width (FB-2 regression) | Owner reported the inline verdict card visibly cut off at the symbol-column's right edge instead of spanning the page width. Card width now capped at 1020px to match `.container` content width and breaks out of the narrow `<td>`. |
| `8b5074a` | perf: cache verdict card DOM and avoid re-fetch on row re-click (FB-2 follow-up) | Owner reported sluggishness re-opening the same ticker on Leader Detector. Verdict card DOM is now cached per-symbol in `_verdictCache`; cache cleared on `loadScreenerMap` (Daily Report Refresh Report). |
| `b118394` | feat: add close button to inline verdict card and strategy chart (FB-2 / FB-8 polish) | Owner asked for an explicit close affordance — re-click-same-row was undiscoverable. New `.detail-inline-close` × button in the card's top-right; modal mode keeps its existing × (unchanged). |
| `8a550ac` | feat: show loading state in inline verdict card while fetching (FB-2 polish) | Verification round 2 surfaced silent latency on uncached Daily Report clicks. `_renderDetailLoading` now paints a spinner + "Loading {sym} fundamentals…" band immediately on click; cache-hit path short-circuits to skip the placeholder for instant feel (also closes item 15). |
| `090a3c4` | fix: × button now renders on Daily Report inline verdict card (FB-2 regression) | Verification round 2 caught the × button missing on Daily Report after the inline-card width cap landed. New `.detail-sticky-wrap` shared-sticky container pins host + button to the visible viewport; also closes the Leader Detector close-button drift edge (item 19). |
| `fdb32ef` | perf: drop redundant pre-fetch focus + scrollIntoView in inline detail open (FB-2 critical, prep) | Owner reported multi-second browser-wide freeze on Leader Detector ticker row click. First of two staged perf commits — removes the pre-fetch `host.focus()` + `host.scrollIntoView()` calls, cutting two of the four synchronous layout flushes per click. Trivial revert if needed. |
| `213720e` | perf: eliminate synchronous freeze on Leader Detector row click (FB-2 critical) | Second staged perf commit. Makes `.detail-sticky-wrap` conditional — Leader Detector path renders WITHOUT the sticky wrap (close button anchors to `<td.detail-cell>` directly via new `.detail-cell > .detail-card-host` direct-child CSS). Daily Report and Strategy Lab keep the sticky wrap. Removes the `position:sticky` descendant that was forcing Blink to recompute the sticky containing rect against the inner scroll container on every layout flush across all 12,700 cells. |
| `7edc7bb` | perf: drop post-mutation focus call from `closeDetail` to fix close-path freeze (FB-2 verification round 4 follow-up) | Verification round 4 surfaced a multi-second freeze on the **close** path symmetric to the open-path freeze that `213720e` fixed. Removed the post-mutation `symCell.focus({preventScroll:true})` call from `closeDetail()` (was at line 1807-1813). Symbol cell has no `tabindex` so the call was a functional no-op, but it forced a synchronous layout flush in Blink that re-measured all 12,700 cells of the 1,414-row Leader Detector auto-layout table after the row removal. Cuts sync layout-flush triggers on the close path from 2 to 1; the remaining `removeChild` invalidation defers to the natural rAF tick. The click-another-ticker case (`closeDetail` → `openSymbolDetail`) now batches both row removal and new row insertion into a single rAF reflow. Sophia LGTM with notes — see deferred items 29 + 30. |

### Additional deferred items from this patch round

Sophia's review of the three patches surfaced more 7d follow-ups. Numbering continues from the eleven above.

12. **Inline card width cap is intentional** — capped at 1020px to match `.container` content width. On Leader Detector with a wide table, the card visibly stops short of the table's right edge. Documented behaviour, not a bug.
13. **Patch `aec5e05` cross-browser** — browser-test on Windows Firefox (scrollbar gutter math differs), at 1060px viewport (cap crossover), and at Windows display scaling 125% / 150%.
14. **Cache staleness on Rebuild Now** — `loadLeaders(true)` does NOT call `loadScreenerMap`, so `_verdictCache.clear()` never fires after a leader rebuild. Re-clicking a previously-previewed ticker shows pre-rebuild verdict data with no visual cue. 7d fix: clear `_verdictCache` in the rebuild-done branch, OR have `loadLeaders(true)` re-pull the screener map. Not a blocker — rebuild is a 3.5h operation rarely triggered casually.
15. **Cache-hit placeholder flash** — cache-hit path still renders `_renderDetailLoading(host, symbol)` for one frame before painting cached card. Skip the placeholder if `_verdictCache.has(symbol)` for instant feel. — **Resolved by `8a550ac`** (the `isCached` short-circuit in `openSymbolDetail` skips `_renderDetailLoading` on cache hits).
16. **Close-button font-size deviation** — `.detail-inline-close` uses 18px; `.app-banner .ab-close` uses 16px. 7d: bump to 16px to match exactly, OR document the deviation in PATTERNS.md.
17. **Close-button tap-target a11y** — bump `.detail-inline-close` padding to `4px 10px` to clear WCAG 2.5.5 24×24 tap target.
18. **INSUFFICIENT_DATA header crowding** — on INSUFFICIENT_DATA verdict cards, the reason-code chip and the close × occupy the same top-right band. Either right-pad the header `padding-right:32px` when inline, or shift × to `right:24px` so it clears the chip.
19. **Issue 3a: Leader Detector close-button drift** — on Leader Detector specifically (table inside `<div overflow-x:auto>`), if table content width exceeds the viewport AND the user has not horizontally scrolled, the `position:absolute; right:18px` close button on the `<td>` lands at the td's true right edge — past the visible card area. Daily Report and Strategy Lab don't hit this because their `.tbl-wrap { overflow:hidden }` clips the cell to the page-content width. 7d structural fix: wrap host + button in a shared sticky container. — **Resolved by `090a3c4`** (new `.detail-sticky-wrap` shared-sticky container pins both host and × button to the visible viewport; also fixes Daily Report's user-reported missing × button manifestation).
20. **Row click lacks `aria-expanded`** — clickable `<tr>` has no `aria-expanded` reflecting open/closed state. Future a11y pass (companion to items 2, 3, 8).

### New deferred items from verification round 2 patches

Sophia's review of `8a550ac` and `090a3c4` surfaced six more 7d follow-ups.

21. **Spinner band has no `role="status"` / `aria-live="polite"`** — screen-reader users get no audible feedback during the loading wait. Applies to all 5 `loaderHTML` callers, not just inline detail. 7d fix: wrap loader text in a live region or mark `.detail-card-host` as `aria-live="polite"`.
22. **Spinner has no `prefers-reduced-motion` override** — `.spinner` keeps spinning even when the OS requests reduced motion. Low urgency. Fix at the global `.spinner` rule (~line 246-247) so all loaders inherit the override.
23. **Loading copy could disambiguate SEC source** — "Loading AAPL fundamentals…" could later evolve to "Fetching latest filings for AAPL…" to set expectation that this is fresh SEC data, not a stale cache. Optional copy polish.
24. **Strategy Lab close-button visual QA after `.detail-sticky-wrap`** — confirm in a real browser that `.detail-inline-close` on Strategy Lab still lands cleanly at the card's top-right corner now that `.detail-sticky-wrap` adds `padding:0 8px` (button anchors to the wrap's padding-box edge, ~8px further left than before).
25. **Escape closes modal but not inline-mode card** — existing limitation, not introduced by `090a3c4` (already item 1 in the deferred list). Logged here for visibility during round-2 patch review.
26. **Narrow-desktop QA at ~720px viewport** — just above the 700px modal breakpoint, `100vw-40px ≈ 680px` clamps the wrap correctly; verify the verdict card's internal layout doesn't horizontally overflow at that width.

### New deferred items from the perf-fix review

Sophia's review of `fdb32ef` + `213720e` surfaced one cross-tab visual inconsistency. Wright captured a deferred escalation lever.

27. **Cross-tab close-button visual offset on Leader Detector** — after `213720e`, the × button on Leader Detector sits ~8px further from the card's visible right edge than on Daily Report. Wrapped path (Daily Report / Strategy Lab): button anchors to `.detail-sticky-wrap` (which has `padding:0 8px`), × lands ~26px from the cell edge. Unwrapped path (Leader Detector): button anchors to `.detail-cell` directly at `right:18px`; the host's 8px padding pulls the visible card edge in but the button doesn't follow. 7d fix: either move the 8px padding back onto a wrapper element on the Leader Detector path, or anchor the × button to a wrapper whose inset matches the host's padding.
28. **Defer `table-layout:fixed` on the leader table (escalation lever)** — only ship if `213720e` does not fully resolve the freeze in browser verification. Auto layout is what makes 1,414 rows × 9 cols expensive to re-measure; fixed layout + explicit column widths (~line 2937) removes the per-insert column re-measure. Gated on a Chrome DevTools Performance profile that confirms the remaining cost is column re-measure, not something else — the visual-regression surface (every column needs a width) is large enough that we want evidence first.

### New deferred items from the close-path perf-fix review (`7edc7bb`)

Sophia LGTM'd `7edc7bb` with two a11y follow-ups that are now unblocked / surfaced by removing the focus call. Cross-referenced to existing items 1, 2, 3 (the symbol-cell a11y trio).

29. **Symbol cell needs `tabindex="0"` + `role="button"` + Enter/Space keyboard handler** — direct cross-ref to existing items 2 + 3. The `7edc7bb` removal of the post-close `.focus()` call makes this gap purely a keyboard-discoverability issue rather than a paying-cost-for-no-effect issue. Once the cell becomes focusable, focus-restore can be re-introduced (cheaply, scoped just to the just-closed row) without re-triggering the freeze, since the layout flush is paid once at close-time on a row that is already in the viewport.
30. **Modal Esc-close should restore focus to the trigger row** — once item 29 lands and the symbol cell is focusable, the modal-mode Escape handler should call `triggerRow.querySelector('.symbol-cell').focus()` on close so keyboard users return to where they were. Pairs with existing item 1 (inline mode has no Escape-to-close at all). Both blocked behind item 29; do not implement focus-restore until the cell is focusable, otherwise we're back to the no-op-with-cost shape that `7edc7bb` just removed.

---

## Regression B investigation — out of scope

**Status: investigated, NO frontend commit on Round 7a. Awaiting user authorization for a backend follow-up round.** Wright AGREE with skipper diagnosis.

**User report.** Owner reported a ~50-second recompute delay on the Ticker Lookup tab during verification round 4. User hypothesised the cause was a recent Round 7a commit — specifically `b118394` — that may have inadvertently broken or removed a frontend prediction cache.

**Diagnosis (server-side, not frontend).**

- **Primary cause:** `api_server.py:488` cache-freshness gate rejects daily reports older than 22 hours. The latest dual report on disk at investigation time was `dual_report_20260424_1842.json`, approximately 22h 48m old. The 22h gate evaluated the report as stale, the cache returned `None`, and `/api/predict-compare` fell through to a fresh ~50-second compute.
- **Secondary cause:** `_load_latest_report_from_disk()` (api_server.py:478-482) is only invoked from `/api/report`, never from `/api/predict-compare`. After a server restart, if the user opens Ticker Lookup BEFORE opening the Daily Report tab, the in-process `_report_cache["data"]` is `None`, so `_get_cached_compare_result` has nothing to read even when a fresh dual-report JSON exists on disk. Cold-start path always hits the 50s compute regardless of disk freshness.

**User hypothesis disproven.** `b118394` was independently re-verified to be UI-only — the diff is 29 lines, adds `.detail-inline-close` × button to the inline verdict + chart cards, and contains zero references to caching, fetch, predict-compare, or any backend endpoint. Cross-checked `git log main..agent-round7a -- api_server.py` and the result is empty: Round 7a has not touched `api_server.py` in any commit. The 50s recompute behaviour exists on `main` and on `agent-round7a` identically.

**No frontend prediction cache exists.** Searched the entire frontend across all branches — there is no in-memory or storage-backed prediction cache for `/api/predict-compare` results. Adding one in Round 7a would be the speculative caching the owner explicitly forbade and would mask the real server-side issue rather than fix it. The data is already cached on disk in `dual_report_*.json`; the gap is that `/api/predict-compare` does not consult it correctly.

**Why no frontend fix is appropriate.**

- The disk-side data is fresh (the report exists, the compute already ran during the most recent Daily Report).
- A frontend cache would diverge from server truth on rebuild and would need its own invalidation contract — net new complexity for no real benefit.
- The two-line server fix is surgical and lives in the right layer.

**Recommended action — Wright recommendation A: stop, document, escalate.** The fix lives in `api_server.py`. Round 7a is scoped frontend-only. Round 7a closes here on Regression B with a clean handoff. Backend follow-up round should:

1. **`api_server.py:488`** — widen the 22-hour freshness gate. Suggested: read from disk and accept the latest dual report regardless of age, with a warning log if older than N hours; OR raise the gate to 48h to bridge the typical "report ran yesterday afternoon, opening it next morning" cadence.
2. **`api_server.py:478-482`** — add a `_load_latest_report_from_disk()` call inside `_get_cached_compare_result` when `_report_cache["data"]` is `None`, so cold-start hits disk before falling through to fresh compute.

See the **ASK** section at the top of this document for the authorization request.

---

## Diagnosis: Leader Detector click freeze

**Symptom (owner-reported).** Clicking a ticker row on the Leader Detector tab froze the entire browser for several seconds — and bled into other apps on the machine remaining unresponsive during the freeze. Network was ruled out (cache hits froze too).

**Trace (skipper, confirmed by wright).** Pure main-thread layout starvation in Blink's renderer, not network or JS work:

- Leader Detector renders a 1,414-row `<table style="width:100%">` (auto layout) × 9 columns = ~12,700 cells.
- The table sits inside NESTED scroll containers — `.tbl-wrap` wraps an inner `<div overflow-x:auto>`.
- Pre-`213720e`, every inline open inserted `.detail-sticky-wrap` (a `position:sticky` descendant) into a `<td>` inside that nested-scroll structure. On every layout flush, Blink had to recompute the sticky element's containing rect against the inner scroll container, which forced a re-measure across all 12,700 cells of an auto-layout table.
- The open path triggered FOUR synchronous layout flushes per click — `host.focus()` × 2 and `host.scrollIntoView()` × 2 (one pre-fetch pair, one post-fetch pair). Each flush paid the full sticky+auto-layout re-measure cost.
- Multi-second freeze affecting other apps is consistent with the renderer process saturating a CPU core long enough to block compositor + IPC timers.

Wright reviewed and confirmed the diagnosis; specifically called out the nested scroll container as the amplifier — without it, sticky containing-rect math is local; with it, every flush walks the full table.

**Fix shape (two-commit, sequenced per wright).**

1. `fdb32ef` drops the pre-fetch `focus()` + `scrollIntoView()` pair. Halves the per-click sync flush count from 4 to 2. Surgical, trivially revertable.
2. `213720e` makes `.detail-sticky-wrap` conditional. `_mountInlineDetail` checks `clickedRow.closest('#leadersTblWrap')`:
   - **Leader Detector** — renders WITHOUT the sticky wrap. The × button anchors to `<td.detail-cell>` directly (cell already has `position:relative`). New CSS rule `.detail-cell > .detail-card-host` (direct-child selector) carries the same 1020px max-width / `min(100vw - 40px, …)` viewport clamp that previously lived on the wrap, so the card's visual width is preserved.
   - **Daily Report / Strategy Lab** — keeps `.detail-sticky-wrap` unchanged (their content can exceed 1020px and they need the sticky-pin behaviour for the close button).

**Why this works.** Removing the `position:sticky` descendant from the Leader Detector path eliminates the per-flush sticky-containing-rect recompute against the nested scroll container — the dominant cost. Combined with the halved sync-flush count from `fdb32ef`, the freeze should drop from multi-second (whole-machine impact) to a few hundred ms or less (perceptible but not blocking).

**Trade-off (sophia + wright accepted).** At extreme zoom (>150%) where the leader table horizontally overflows its `overflow-x:auto` parent, horizontal scrolling moves the × button (which is now anchored to the `<td>`, not the viewport-pinned wrap). The just-clicked row stays as the visual anchor so users can navigate back. Acceptable for the tail-case zoom; the multi-second freeze for the common case was not.

**If `213720e` doesn't fully resolve.** Wright's deferred Option B (item 28 below): add `table-layout:fixed` + explicit column widths to the leader table. Auto layout is the row-count multiplier; fixed layout removes the per-insert column re-measure. Capture a Performance profile first to confirm before adding visual-regression risk.

---

## Verification status

| Check | Result | Notes |
|---|---|---|
| Code-read trace (skipper) | DONE | Full dispatcher + close path + sort/tab guards walked. |
| Wright re-review | LGTM v2 | 7 v1 concerns addressed; no new BLOCKers. |
| Sophia review — Phase 2 (FB-2) | LGTM with notes | 9 follow-ups deferred to 7d, none blocking. |
| Sophia review — Phase 3 (FB-8) | LGTM with notes | Chart UX edges deferred to 7d. |
| `[hidden]` CSS pairs (P-1) | VERIFIED | Modal overlay + card have `[hidden]` rules; inline classes are create/destroy and exempt. |
| Backend tests | UNCHANGED | No backend edits this round. |
| **Browser click-through** | **DEFERRED to owner** | See below. |

### Manual click-through (owner step)

Hard-refresh after each commit before testing the next.

- **Daily Report** — click any ticker row; verdict card expands inline beneath that row. Click a second row; first collapses, second expands. Sort any column while a card is open; card collapses.
- **Leader Detector** — same three checks.
- **Strategy Lab** — click any library row; chart card expands inline beneath that row. Cached symbols render instantly; uncached take 4-8 minutes (spinner during).
- **Tab switch with a card open** — card collapses.
- **Responsive** — resize browser to <700px and click a row; modal overlay engages (chart card or verdict card depending on tab).

---

## Verification round 2

**Status: completed — two issues found, both addressed by `8a550ac` and `090a3c4`.**

Original three round-2 scenarios (kept as historical context for the verification chain):

1. **Daily Report row click** — verdict card spans the visible content width (≤1020px), no right-edge clipping. Sort and scroll work as before.
2. **Leader Detector row click** — card appears with reasonable latency on first click; cache makes subsequent clicks of recently-opened tickers feel ~instant. Cache invalidates on **Refresh Report** (Daily Report tab) but **NOT** on **Rebuild Now** — known 7d follow-up (item 14 above).
3. **Either tab — close button** — clicking the × button in the card's top-right collapses it. Re-clicking the row still toggles too. Modal mode (below 700px viewport) uses the modal's own × button (unchanged).

Issues caught during round 2:

- **Silent loading latency on uncached clicks** — clicking a Daily Report / Leader Detector row showed nothing for several seconds while the SEC fetch resolved. Owner could not tell whether the click had registered. → addressed by `8a550ac` (loading skeleton paints immediately on click; cache-hit path skips placeholder).
- **× button missing on Daily Report inline verdict card** — the `b118394` × button rendered correctly on Strategy Lab and (sometimes) Leader Detector but not at all on Daily Report after the `aec5e05` width cap landed. → addressed by `090a3c4` (new `.detail-sticky-wrap` shared-sticky container).

---

## Verification round 3

Re-test these scenarios after pulling `agent-round7a` (hard-refresh between tabs):

1. **Daily Report row click** — loading skeleton (spinner + "Loading {sym} fundamentals…") appears immediately on click, then verdict content paints when fetch resolves.
2. **Leader Detector row click** — loading skeleton appears on FIRST click of each new ticker; cached re-clicks paint instantly with NO spinner flash.
3. **Daily Report row click** — × button visible top-right of the verdict card; clicking × closes the card.
4. **Click cycle stress test on all three surfaces** (Daily Report, Leader Detector, Strategy Lab): row 1 → ×, row 2 → ×, row 3 → ×. No regressions; sort and tab-switch still close inline cards.

---

## Verification round 4

Re-test these scenarios after pulling `agent-round7a` (hard-refresh between tabs). Focus is the Leader Detector freeze fix (`fdb32ef` + `213720e`) plus regression coverage on the two unchanged paths.

1. **Leader Detector — click a ticker row** — verdict card paints with no perceptible browser freeze. Other apps (e.g. file explorer, terminal) remain responsive throughout the click. This is the primary fix.
2. **Daily Report — click a ticker row** — no regression: card spans the visible content width (≤1020px), × button visible top-right, sort / scroll / row-swap continue to behave as in rounds 2-3.
3. **Strategy Lab — click a library row** — no regression: chart card expands inline beneath the row, × button visible, cached/uncached paths both render correctly.
4. **Either tab — modal mode (≤700px viewport)** — no regression: shrink window below 700px, click a row, modal overlay engages with its own × button (the conditional sticky-wrap logic only affects inline mode).

---

## Verification round 5

Re-test these scenarios after pulling `agent-round7a` (hard-refresh between tabs). Focus is the close-path freeze fix (`7edc7bb`) plus the Ticker Lookup recompute behaviour clarification.

1. **Leader Detector — close-path freeze fix.** Open the tab, click a ticker row to expand the verdict card. Then:
   - **Close via × button** — click the inline × button on the open card. The card collapses with NO perceptible browser freeze. Other apps (file explorer, terminal) remain responsive throughout the close.
   - **Close via row-swap** — with one card open, click a DIFFERENT ticker row. The first card collapses, the second opens, and the entire transition completes without a freeze (close + open are batched into a single rAF reflow).
2. **Ticker Lookup — recompute is server-side, not a Round 7a regression.** Behaviour depends on the freshness of the latest `dual_report_*.json` on disk:
   - **Fresh report (within 22 hours of generation)** — expect cache hit. The CACHED chip should be visible and the result should appear near-instantly.
   - **Stale report (≥22 hours old)** — expect a ~50-second recompute. **This is server-side behaviour from `api_server.py:488` and is NOT a Round 7a regression.** If the owner sees a recompute on a fresh report (verified <22h old by file mtime), escalate — that is a different bug.
   - Authorization for the backend fix is requested in the **ASK** section at the top of this document.
3. **No regressions on Daily Report or Strategy Lab.** Click a ticker row on Daily Report and a library row on Strategy Lab; both should expand inline without freeze, the × button visible, sort / row-swap / tab-switch all behave as in rounds 2-4.

---

## Session end

Two features shipped, two commits on `agent-round7a` (plus this summary). Eight post-merge patches followed (`aec5e05`, `8b5074a`, `b118394`, `8a550ac`, `090a3c4`, `fdb32ef`, `213720e`, `7edc7bb`) addressing owner-reported clipping, perf, discoverability, loading feedback, Daily Report close-button visibility, the Leader Detector open-path multi-second click freeze, and the close-path freeze. Branch is ready for owner verification round 5, then merge to `main`. Phase 7d picks up the thirty deferred a11y/polish items above. Regression B (Ticker Lookup ~50s recompute) is documented as out-of-scope for Round 7a and **awaits user authorization for a backend follow-up round** to fix `api_server.py:488` (22h freshness gate) and `api_server.py:478-482` (cold-start disk-cache miss).
