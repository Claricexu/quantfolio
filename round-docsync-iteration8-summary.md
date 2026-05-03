# Doc-sync round — Iteration 8 close summary

Date: 2026-05-02. Branch: `agent-docsync-iteration8`, four commits ahead of `b1bf10b` (the last Round 8d feature commit) plus this docs commit. Not pushed, not merged — ready for owner merge. Three-agent team across four phases: **wright** owned Phase 1 (structural review against actual code state), **skipper** owned Phases 2 and 4 (implementation + recovery from a misattributed amend), **sophia** owned Phase 3 (user-facing prose review on USER_GUIDE).

This round is the documentation reconciliation that closes Iteration 8 (Rounds 8a-8d). Iteration 8 shipped: timezone fix + SVR consolidation + a11y bundle (8a), email logic + peer SVR column (8b), manual alert trigger + Send Email button (8c), biweekly auto-refresh + Case B insufficient-data handling (8d). None of that landed in user-facing or developer-facing docs at feature time; this round consolidates four files in four commits.

---

## What shipped

| Commit | Title | Diff |
|---|---|---|
| `7a9f82f` | docs: README sync for Iteration 8 (alert rules, manual trigger, auto-refresh) | README.md +21 / -3 |
| `9e98c23` | docs: USER_GUIDE sync for Iteration 8 (manual send, peer SVR, biweekly refresh, TTL bump) | USER_GUIDE.md +18 / -11 |
| `6b399e3` | docs: DEVELOPMENT sync for Iteration 8 (biweekly job, TTL bump, alert classifier, test count) | DEVELOPMENT.md +11 / -5 |
| `36162c5` | docs: CHANGELOG entries for Rounds 8a-8d | CHANGELOG.md +29 |
| _(this commit)_ | docs: doc sync summary for Iteration 8 close | this file |

Aggregate diff vs `b1bf10b`: 4 files, +79 / -19. Tree clean. No push, no merge — gating is the owner's review of this summary.

No code changes this round. All four commits are doc-only; the application surface is untouched.

---

## Phase 1 — wright structural review

Wright walked all four target files against the current code/feature state and surfaced **26 sync items**, organized per file:

- **README.md (6 items)** — alert rule prose mentioned only the legacy single-strategy path, omitted Case B handling, didn't mention the manual trigger endpoint or the Send Email button, didn't mention the biweekly auto-refresh job, and didn't reflect the TTL bump.
- **USER_GUIDE.md (8 items)** — Send Email Alert button + confirmation dialog flow not documented, peer median SVR column missing from the Daily Report email walkthrough, biweekly refresh cadence not mentioned, Case B "insufficient data, retry in 8 weeks" user-facing prose missing, alert-rule section was stale, troubleshooting section had a residual 7-day TTL claim, ET marker convention not noted, manual-send permission/auth boundary not described.
- **DEVELOPMENT.md (6 items)** — biweekly cron job (Friday 9 PM ET) not described, TTL constant bump not reflected, `classifyAlertRow` predicate not surfaced as the canonical alert classifier, test count stale, manual trigger endpoint not in the API surface table, state-file artifact not in the storage section.
- **CHANGELOG.md (4 items)** — no entries for Rounds 8a, 8b, 8c, or 8d.

### Three corrections wright caught against the original prompt

The owner's prompt to wright had three minor inaccuracies that wright verified against actual code/git state and surfaced before implementation:

1. **Endpoint name.** Prompt said `/api/alerts/send`; actual route is `/api/alerts/send-manual`. Confirmed via grep against `api_server.py`.
2. **Test count.** Prompt said "~98 tests"; actual count is **108** (verified by running the non-yfinance subset). Iteration 8 added the alert-classifier and biweekly-scheduler tests that the prompt's mental model missed.
3. **Biweekly cron schedule.** Prompt said "weekly"; actual cadence is **biweekly, Friday 9 PM ET specifically** (per `c9e4ebc`). Wright pulled the exact cron expression rather than paraphrasing.

All three corrections folded into wright's structural review before skipper began implementation.

---

## Phase 2 — skipper implementation

All 26 items landed across the four commits in the order shown above. Three modifications relative to wright's proposal, each with reason:

1. **Timezone-fix commit hash corrected.** Wright's proposed CHANGELOG entry referenced commit `238fe4c` for the Round 8a timezone fix. Skipper cross-checked against `git log` and the actual hash on this branch is **`ebca265`** (the prior `238fe4c` reference appears in `round8a-summary.md` and reflects an earlier branch state pre-rebase). Corrected in the CHANGELOG entry.
2. **DEVELOPMENT §8.1 prose retained.** Wright proposed bumping an aggregate count in §8.1; on inspection there was no aggregate count in the section to bump (the count lives only in §3 and was updated there). Section §8.1 prose was left as-is.
3. **USER_GUIDE B8 — only one residual 7-day claim.** Wright flagged the TTL-bump sweep against Part 11 (troubleshooting). Skipper grepped the file and found the remaining 7-day claim in **Part 12, not Part 11**. Fixed in Part 12; Part 11 was already clean.

Net per-file: README +21/-3, USER_GUIDE +18/-11, DEVELOPMENT +11/-5, CHANGELOG +29.

---

## Phase 3 — sophia review

Sophia reviewed the user-facing prose changes (USER_GUIDE primarily) and **BLOCKed** on the alert-rule section with three concrete issues:

1. **Line ~231 — enum jargon leakage.** Skipper's prose pulled internal route enum values (`pro_buyonly`, `lite_full`, `best_strategy`, `pro_full`, `lite_buyonly`) directly into the user-facing rule description. These are engineer-facing identifiers from `classifyAlertRow`, not labels the user sees in the UI. Sophia's lens: a non-engineer reading the rule has no way to map "pro_buyonly" to anything they recognize.
2. **Line ~234 — "validated single-model paths" is engineer-coined.** The phrase is precise within the team but doesn't exist as a UI label or anywhere the user encounters. It reads as a system-internal classification.
3. **Line ~590 — "Case B" codename dropped without translation.** Round 8d's "Case B" is internal shorthand for "ticker had insufficient data; retry in 8 weeks." Dropping the codename into user prose without translating leaves the user unable to act on it.

Sophia provided a concrete plain-English rewrite for the alert rule, calibrating against the surrounding tone at line ~218 (which uses verdict-language the user already sees in the verdict card: "Strong Buy", "Avoid", etc.). The rewrite preserved the technical precision of the routing logic but expressed it in terms the user can match against their own UI experience.

All three issues fixed in skipper's amend. Sophia re-reviewed: **PASS**.

---

## Phase 4 — recovery from misattributed amend

Honest writeup: the first amend attempt landed the Phase 3 USER_GUIDE fixes onto the wrong commit. What happened, what we did, and the lesson.

### What happened

After sophia's BLOCK, skipper edited USER_GUIDE.md per the rewrite and ran `git commit --amend`. Intent: amend the USER_GUIDE commit (`9e98c23`-equivalent in the original sequence). Actual effect: the amend landed on **HEAD**, which was the CHANGELOG commit (originally `48f4faf`). Result: the USER_GUIDE commit on the branch retained the pre-fix prose, and the CHANGELOG commit's diff stat was inflated with USER_GUIDE changes that didn't belong there.

Owner caught this via a diff-stat verification pass — the CHANGELOG commit's reported file count was 2 instead of 1, which is the giveaway.

### Recovery

Working-tree content was always correct (every fix sophia asked for was on disk); only the commit boundaries were wrong. Recovery did not need any source-file edits.

Steps:

1. `git reset --soft b1bf10b` — uncommitted all four doc-sync commits, kept all changes staged in index.
2. Four per-file commits with the original messages restored:
   - `git reset HEAD .` then stage README only → commit with the original README message → produces `7a9f82f`
   - Same for USER_GUIDE → produces `9e98c23` (now containing the sophia-fix prose, attributed to the correct commit)
   - Same for DEVELOPMENT → produces `6b399e3`
   - Same for CHANGELOG → produces `36162c5`
3. `git log --stat -5` to verify per-file attribution and per-commit diff counts.

The original commit hashes (`f87d62a`, `ae62785`, `6b4da79`, `48f4faf`) are no longer reachable from the branch tip but remain in the reflog for the next 90 days per default `gc.reflogExpire`. No data loss.

### Lesson

`git commit --amend` only ever amends **HEAD**. To amend an earlier commit on a branch, you need a different mechanism — interactive rebase (`git rebase -i`) with `edit` on the target commit, or what we did: `git reset --soft <pre-sequence-base>` followed by per-file recommits with original messages. The soft-reset path is safer when the working-tree content is already correct and the only problem is commit boundaries; rebase is preferable when you need to re-edit content of a single non-HEAD commit in isolation.

If this happens again, skipper's first move should be `git log --stat HEAD~5..HEAD` to inspect commit boundaries before assuming the amend went where intended.

---

## Deferred items rolling forward — owner decision

Two items were intentionally out of scope for this round and surfaced for owner triage at next iteration kickoff:

1. **Per-round summary docs for Rounds 8b, 8c, 8d.** Only Round 8a has a `round8a-summary.md` at the repo root. Rounds 8b/8c/8d shipped without per-round summary docs at the time, and recreating them retroactively was explicitly out of scope for this doc-sync round. Owner will decide whether to backfill them or to treat this Iteration 8 close summary as the consolidated record.
2. **`PROJECT_STATUS.md`** — last updated Round 7c-2 on 2026-04-27. Stale across all of Iteration 8. Owner deferred to next iteration kickoff so the refresh can include forward-looking Iteration 9 priorities rather than just a backward-looking Iteration 8 reconciliation.

Both items are owner-discretion; this round did not touch either.

---

## Verification

- `git diff --stat b1bf10b..HEAD` → 4 files changed, 79 insertions(+), 19 deletions(-). Matches per-commit sum: README +21/-3, USER_GUIDE +18/-11, DEVELOPMENT +11/-5, CHANGELOG +29/-0 = 79/-19.
- `git status` → clean.
- Branch `agent-docsync-iteration8` not pushed. Owner runs the merge sequence (push → PR → merge) separately per the project's shared-state confirmation policy.

---

## Files touched (all four commits + this docs commit)

- `README.md` (+21 / -3 — alert rule rewrite covering the new routing + manual trigger + Send Email button + biweekly auto-refresh + TTL bump)
- `USER_GUIDE.md` (+18 / -11 — manual send flow with confirmation dialog, peer median SVR column, biweekly refresh cadence, Case B user-facing prose, residual 7-day TTL fix in Part 12, alert rule plain-English rewrite per sophia)
- `DEVELOPMENT.md` (+11 / -5 — biweekly Friday-9PM-ET cron job, TTL constant bump, `classifyAlertRow` as canonical alert classifier, test count 108, manual trigger endpoint in API surface, state-file artifact)
- `CHANGELOG.md` (+29 — entries for Rounds 8a, 8b, 8c, 8d with corrected timezone-fix hash `ebca265`)
- `round-docsync-iteration8-summary.md` (this file, new)

No source files touched. No CSV regeneration. No schema migration. Iteration 8 is closed pending owner merge.
