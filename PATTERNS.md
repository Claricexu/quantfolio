# Quantfolio — Engineering Patterns

A living reference for coding patterns to follow or avoid in this repo. Distilled from real bugs, close calls, and design decisions across multiple rounds. Each entry explains the pattern, why it matters, and where it has bitten us before.

Add entries as patterns emerge. When writing prompts for future agent rounds, reference relevant patterns inline so the agent applies them proactively.

---

## UI / Frontend

### P-1 — Respect the HTML `hidden` attribute when defining CSS `display`

**Pattern:** Whenever a UI element uses the HTML `hidden` attribute for its default-hidden state, any CSS rule that sets `display` on that element **must** also include a companion rule targeting `[hidden]` that sets `display: none`.

**Why it matters:** In CSS, an explicit `display` value (e.g. `display: flex`, `display: block`, `display: grid`) overrides the user-agent `display: none` that the HTML `hidden` attribute provides. The element stays visible even when `hidden` is present in the DOM, and any JavaScript that sets `el.hidden = true` becomes a no-op. This creates a silent bug: the markup and JavaScript look correct but the UI doesn't respect the hidden state.

**Correct:**

```css
.app-banner { display: flex; align-items: center; }
.app-banner[hidden] { display: none; }
```

**Incorrect:**

```css
.app-banner { display: flex; align-items: center; }
/* missing [hidden] rule — .app-banner with hidden attribute stays visible */
```

**Where this has bitten us:**
- Round 2 commit `6fe66b0` — Leader Detector rebuild modal was visible on page load because `.modal-overlay { display: flex }` defeated the `hidden` attribute. Cancel / Escape / overlay handlers were never attached because `openRebuildModal()` never ran. Fix: added `.modal-overlay[hidden] { display: none; }`.
- Round 6 H-3 regression (late April 2026) — Pro model availability banner showed even when LightGBM was installed because `.app-banner { display: flex }` defeated the `hidden` attribute, causing the banner to be visible regardless of the `/api/system/status` response. Fix: added `.app-banner[hidden] { display: none; }`.

**Prompt template for future UI rounds:**

> "Any new UI element that uses the HTML `hidden` attribute for default-hidden state must have a `.class-name[hidden] { display: none; }` rule accompanying any `.class-name { display: ... }` rule. This prevents the CSS-override-hidden bug that bit Round 2 and Round 6."

---

### P-2 — Avoid synchronous layout flushes after mutating the leader table

**Pattern:** After DOM mutations to the Leader Detector table (1,414 rows × 9 columns at current universe size), avoid synchronous calls that force browser layout recomputation — `focus()`, `scrollIntoView()`, `getBoundingClientRect()`, or reads of layout properties like `offsetTop`, `offsetHeight`, `clientWidth`. Defer any unavoidable layout-touching work to `requestAnimationFrame` so the browser can batch reflows.

**Why it matters:** When a row is inserted or removed from a large table, Blink (Chromium's rendering engine) marks the table's layout as dirty but defers actual recomputation until something forces a flush. Layout-reading APIs and certain layout-affecting APIs (`focus()` on an element with potential scroll-into-view side effects, `scrollIntoView()` itself) force the browser to synchronously recompute layout for the entire table — re-measuring all 12,700 cells in the leader table at current size. This blocks the main thread for hundreds of milliseconds to several seconds on consumer hardware, manifesting as a UI freeze that also stutters other applications competing for CPU.

The bug class is invisible during code review because each individual `focus()` or `scrollIntoView()` call looks innocuous. The cost only emerges when the call site sits in the dirty-layout window between a DOM mutation and the next natural reflow.

**Correct:**

```javascript
// Mutate first, defer layout-touching work to next frame
function openSymbolDetail(row) {
  const detailRow = createDetailRow();
  row.insertAdjacentElement('afterend', detailRow);
  // Layout-touching work deferred — browser flushes naturally between frames
  requestAnimationFrame(() => {
    detailRow.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });
}
```

```javascript
// Same fix on the close path — no synchronous layout flush after removeChild
function closeDetail() {
  const detailRow = document.querySelector('.detail-row');
  if (!detailRow) return;
  detailRow.parentNode.removeChild(detailRow);
  // No focus(), no scrollIntoView() here — let layout settle naturally
}
```

**Incorrect:**

```javascript
function openSymbolDetail(row) {
  const detailRow = createDetailRow();
  row.insertAdjacentElement('afterend', detailRow);
  detailRow.scrollIntoView();          // forces sync layout flush of 12,700 cells
  detailRow.querySelector('input').focus();  // second sync flush
}
```

```javascript
function closeDetail() {
  const detailRow = document.querySelector('.detail-row');
  if (!detailRow) return;
  detailRow.parentNode.removeChild(detailRow);
  symCell.focus({ preventScroll: true });  // even with preventScroll, this forces a layout flush
}
```

**Where this has bitten:**

- Round 7a verification round 4 — `openSymbolDetail` had pre-fetch `focus()` + `scrollIntoView()` calls that fired before the verdict fetch resolved, causing a multi-second freeze on every Leader Detector row click. Fixed in commit `fdb32ef` by dropping the pre-fetch calls; only one rAF-scheduled `scrollIntoView()` remains, after the fetch completes.
- Round 7a verification round 5 — `closeDetail` had a post-mutation `symCell.focus({preventScroll:true})` that was functionally a no-op (the cell had no `tabindex`) but still forced a layout flush. Removed in commit `7edc7bb`. Closing the detail row now batches naturally with the next animation frame.

**Prompt template for future rounds touching the leader table or any large list view:**

> "When mutating rows in the leader table or any list-style table with hundreds of rows, avoid synchronous layout-flush operations (`focus()`, `scrollIntoView()`, `getBoundingClientRect()`, reads of `offsetTop` / `offsetHeight` / `clientWidth`) immediately after DOM changes. Defer non-critical layout work to `requestAnimationFrame`. Reference PATTERNS.md P-2. The bug from Round 7a verification rounds 4 and 5 cost ~5 hours of debug-and-patch time; do not let it recur."

---

## Backend / Python

*(none yet — add as they come up)*

---

## Testing

*(none yet — add as they come up)*

---

## Git / Workflow

*(none yet — add as they come up)*

---

## How to use this file

**When adding a pattern:**
- Assign a stable ID (`P-N`)
- Describe the pattern in one sentence at the top
- Explain why it matters (the silent failure mode is what's valuable)
- Provide correct and incorrect code examples
- List the specific commits or rounds where it has bitten us
- Provide a prompt snippet that can be pasted into future agent instructions

**When writing prompts for future agent rounds:**
- Scan this file for patterns relevant to the round's scope
- Paste the relevant "Prompt template" section directly into the prompt under a "Required patterns to follow" heading
- This turns tacit knowledge into explicit instructions, reducing the chance of re-discovering the same bug