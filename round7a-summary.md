# Quantfolio round 7a — implementation summary

Branch: `agent-round7a` (2 feature commits ahead of `main`, plus this summary).
Not pushed, not merged. Review / merge decision is yours.

Three-agent team (skipper writes, wright + sophia review). Two feedback items closed: **FB-2** (verdict card inline expansion on Daily Report + Leader Detector) and **FB-8** (strategy comparison chart inline expansion on Strategy Lab library). Three review passes — Wright BLOCKed v1 with 7 concerns, LGTM'd v2 after all were addressed; Sophia LGTM'd both phases with non-blocking notes deferred to Phase 7d.

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

### Additional deferred items from this patch round

Sophia's review of the three patches surfaced more 7d follow-ups. Numbering continues from the eleven above.

12. **Inline card width cap is intentional** — capped at 1020px to match `.container` content width. On Leader Detector with a wide table, the card visibly stops short of the table's right edge. Documented behaviour, not a bug.
13. **Patch `aec5e05` cross-browser** — browser-test on Windows Firefox (scrollbar gutter math differs), at 1060px viewport (cap crossover), and at Windows display scaling 125% / 150%.
14. **Cache staleness on Rebuild Now** — `loadLeaders(true)` does NOT call `loadScreenerMap`, so `_verdictCache.clear()` never fires after a leader rebuild. Re-clicking a previously-previewed ticker shows pre-rebuild verdict data with no visual cue. 7d fix: clear `_verdictCache` in the rebuild-done branch, OR have `loadLeaders(true)` re-pull the screener map. Not a blocker — rebuild is a 3.5h operation rarely triggered casually.
15. **Cache-hit placeholder flash** — cache-hit path still renders `_renderDetailLoading(host, symbol)` for one frame before painting cached card. Skip the placeholder if `_verdictCache.has(symbol)` for instant feel.
16. **Close-button font-size deviation** — `.detail-inline-close` uses 18px; `.app-banner .ab-close` uses 16px. 7d: bump to 16px to match exactly, OR document the deviation in PATTERNS.md.
17. **Close-button tap-target a11y** — bump `.detail-inline-close` padding to `4px 10px` to clear WCAG 2.5.5 24×24 tap target.
18. **INSUFFICIENT_DATA header crowding** — on INSUFFICIENT_DATA verdict cards, the reason-code chip and the close × occupy the same top-right band. Either right-pad the header `padding-right:32px` when inline, or shift × to `right:24px` so it clears the chip.
19. **Issue 3a: Leader Detector close-button drift** — on Leader Detector specifically (table inside `<div overflow-x:auto>`), if table content width exceeds the viewport AND the user has not horizontally scrolled, the `position:absolute; right:18px` close button on the `<td>` lands at the td's true right edge — past the visible card area. Daily Report and Strategy Lab don't hit this because their `.tbl-wrap { overflow:hidden }` clips the cell to the page-content width. 7d structural fix: wrap host + button in a shared sticky container.
20. **Row click lacks `aria-expanded`** — clickable `<tr>` has no `aria-expanded` reflecting open/closed state. Future a11y pass (companion to items 2, 3, 8).

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

Re-test these three scenarios after pulling `agent-round7a` (hard-refresh between tabs):

1. **Daily Report row click** — verdict card spans the visible content width (≤1020px), no right-edge clipping. Sort and scroll work as before.
2. **Leader Detector row click** — card appears with reasonable latency on first click; cache makes subsequent clicks of recently-opened tickers feel ~instant. Cache invalidates on **Refresh Report** (Daily Report tab) but **NOT** on **Rebuild Now** — known 7d follow-up (item 14 above).
3. **Either tab — close button** — clicking the × button in the card's top-right collapses it. Re-clicking the row still toggles too. Modal mode (below 700px viewport) uses the modal's own × button (unchanged).

---

## Session end

Two features shipped, two commits on `agent-round7a` (plus this summary). Three post-merge patches followed (`aec5e05`, `8b5074a`, `b118394`) addressing owner-reported clipping, perf, and discoverability. Branch is ready for owner verification round 2, then merge to `main`. Phase 7d picks up the twenty deferred a11y/polish items above.
