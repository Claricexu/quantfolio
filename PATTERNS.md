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