# Quantfolio — Feature Backlog

A living list of feature ideas collected from real-world usage. Separate from `NEXT_ROUNDS.md` (which is strictly bug-and-correctness work from the audit). This file is where user-driven improvements live before they get scoped into a round.

Add entries here as you notice things during daily use. When a round is ready, pick from here based on impact vs. effort.

---

## High-impact features (worth dedicated rounds)

### FB-1 — Peer benchmarking column on the verdict card

**Requested:** 2026-04-23 (during Round 4 test run)

**Current state:** The verdict card shows two columns — metric name and this company's value.

**Proposed:** Add a third column showing the peer benchmark — what a typical company in the same sector/industry looks like on that metric.

**Why it matters:** A P/E of 18 means very different things depending on whether the peer group averages 12 or 28. Right now users have to know industry norms from memory to interpret the card. Adding a peer column makes the verdict card self-explaining.

**Open questions to resolve before implementation:**
- Peer definition — SIC 2-digit? SIC 3-digit? Existing `broadSector()` buckets? Custom peer groups?
- Peer metric — median, mean, or percentile rank (e.g., "you're in the 72nd percentile of Tech peers")?
- Data source — is peer-aggregate data already in `fundamentals.db`, or does it need a new aggregation step in Layer 1?
- Edge cases — CRWD-type niche companies with thin peer groups. Show "insufficient peers" instead of misleading numbers?
- Backward compat — `/api/screener/{symbol}` response needs a new field. Make it nullable so old frontends don't break.

**Estimated effort:** 3-5 hours (schema, aggregation, UI). Likely its own round.

**Suggested team:** Three-agent (wright for schema design, skipper for aggregation, sophia for UX/peer-metric choice).

**Related audit findings:** None directly. New work.

---

### FB-2 — Verdict card appears inline near clicked ticker (not fixed at top)

**Requested:** 2026-04-23 (during Round 4 test run)

**Current state:** The verdict card renders at a fixed position above tab content on every tab where it appears. When a user clicks a ticker in a long list (e.g., Daily Report or Leader Detector), the verdict appears far from where they clicked — forcing a scroll and visual context-switch.

**Proposed:** When a user clicks a ticker row, expand an inline verdict card immediately below or beside that row. Keep focus on the clicked position.

**Why it matters:** Workflow where the user scans a list and wants to read verdicts in context. Current flow breaks that loop every click.

**Open questions to resolve before implementation:**
- Multi-select — if the user clicks a second ticker, does the first card collapse or stay open? Probably collapse.
- Mobile / narrow viewport — inline expansion can overflow. Is a modal overlay acceptable as a fallback, or strict inline?
- Which tabs — Ticker Lookup already has the card inline (via the Predict flow). This request is primarily about Daily Report, Leader Detector, and possibly Strategy Lab.
- Scroll behavior — should the clicked row auto-scroll into view when the card expands below it?

**Estimated effort:** 1-2 hours. Pure frontend, small scope.

**Suggested team:** Single agent, sophia preferred.

**Related audit findings:** None directly. Adjacent to Round 2's verdict-card unification work (C-1, H-1).

---

## Medium-impact features (consider for batched rounds)

### FB-3 — "N tickers temporarily unavailable" footer chip

**Requested:** 2026-04-23 (discovered during Phase 4.3 review of Round 4)

**Current state:** Phase 4.2 plan in ITERATION_PLAN.md called for surfacing `rate_limited_skips: int` in the scan summary JSON, but the field didn't actually land in code. Round 4 now retries rate-limited requests properly, but users have no visible indicator when tickers were ultimately skipped after retry exhaustion.

**Proposed:** Add `rate_limited_skips: int` to the scan summary returned by `/api/report`. Render as a subtle footer chip on the Daily Report tab: "N tickers temporarily unavailable" with a tooltip explaining rate-limit semantics. On zero, hide the chip entirely.

**Why it matters:** Round 4 fixed the silent-drop behavior at the fetch layer (the code now raises instead of returning empty), but the user-visible half never shipped. Without this chip, users still can't tell "this ticker is broken" from "this ticker was rate-limited today."

**Estimated effort:** ~10 lines in api_server.py to thread the counter through the scan summary, plus ~20 lines in frontend/index.html for the chip and tooltip. Single commit.

**Suggested team:** Single agent, skipper.

**Related audit findings:** C-5 (completed the fetch-layer half in Round 4 Phase 4.2, this finishes the UX half).

---

## Small tweaks (quick wins)

*(none yet — add as they come up)*

---

## How to use this file

**When adding an entry:**
- Assign a stable ID (`FB-N`) so rounds can reference it
- Date stamp the request
- Distinguish "what I see now" from "what I want" — makes the scope obvious
- List open questions explicitly — this is future-you's debugging hint
- Estimate effort honestly; it's fine to be wrong

**When picking items for a round:**
- Check ITERATION_PLAN.md for the current scope
- Features go into *feature* rounds, not bug-fix rounds
- Round 7 (UX cleanup) from NEXT_ROUNDS.md is for audit-finding UX polish, not new features — keep them separate

**When an entry ships:**
- Move it to a "Shipped" section at the bottom with the commit hash and date
- Link to the CHANGELOG entry
- Note any scope that was deferred to a follow-up FB entry

---

## Shipped

*(empty — will populate as features land)*
