# Quantfolio — Inbox

Quick capture of anything I notice while using Quantfolio. Don't think about where it belongs yet — just write it down fast and move on.

Triage this inbox every week or two:
- Features → `FEATURE_BACKLOG.md` (assign an FB-N id)
- Bugs → `audit-findings.md` or a new section there
- Engineering patterns → `PATTERNS.md` (assign a P-N id)
- Already-fixed-in-my-head → delete the entry

Format is whatever's fastest. Date-stamp helps recall later.

---

## Unsorted

<!-- Add entries below. Format: "- YYYY-MM-DD: [what you noticed]". Context optional. -->

- 

---

## Format examples (delete once you've got the hang of it)

- 2026-05-01: Leader Detector table scrolls jumpy on mobile Safari
- 2026-05-02: Predicting CRWD three times in a row gave different confidences each time — is that cache invalidation or intended randomness?
- 2026-05-03: Want a "copy as JSON" button on the verdict card so I can paste it into other notes
- 2026-05-03: Daily Report banner says "24 min" but it took 7 min — great, but worth re-measuring and tightening the estimate
- 2026-05-10: Strategy Lab "Run All" progress bar disappears during the last 10% — looks frozen

---

## Notes on triaging

When you sit down to triage (every 1-2 weeks, or before planning a round):

**For each entry, ask:**
1. Is this a feature, a bug, or a pattern?
2. Does it belong in an existing category, or is it something new?
3. Is it a "do once it matters" thought or "do when I next touch this area"?

**Move the entry to the right file with a proper format:**
- Features get `### FB-N — one-line title` with current state / proposed / why / open questions / effort
- Bugs get recorded in `audit-findings.md` under a new `## Post-audit findings` section if it doesn't exist yet
- Patterns get full P-N treatment in `PATTERNS.md`

**Delete the original entry from this inbox** once it's been moved.

**If an entry has sat here for more than 30 days without being triaged**, it's probably not important to you. Delete it without moving it.
