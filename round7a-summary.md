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
5. **No discoverability hint above tables** — Daily Report relies on the existing `title="Click for fundamental detail"` tooltip only.
6. **Resize-while-open keeps current mode** — no auto-switch between inline and modal on viewport-cross. Documented in code comment; Wright judged auto-switch premature for 7a.
7. **Library row hint omits the 4-8 minute first-time backtest wait** — copy update.
8. **Library row `<tr onclick>` lacks ARIA** — no `role="button"` / `aria-expanded` / keyboard handler.
9. **Active-row + detail-row visual seam** — two adjacent shaded greys with a dashed border between them. Functional but busy.
10. **Library poll race window** — the 3000ms interval can fire one more time between user-close and the next tick. Harmless (symbol-match guard catches it) but not deterministic.
11. **Legacy `.detail-card-wrap` CSS rules unreferenced** — left in place to keep this round's diff focused. Cleanup sweep candidate.

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

## Session end

Two features shipped, two commits on `agent-round7a` (plus this summary). Branch is ready for owner browser click-through, then merge to `main`. Phase 7d picks up the eleven a11y/polish items above.
