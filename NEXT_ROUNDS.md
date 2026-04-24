# Quantfolio — Next Rounds Memo

A handoff for future agent work, written immediately after shipping Rounds 1–3 and the doc-sync round. Current main is at commit `11fe90f`.

This memo is organized by priority. Higher-priority items earn their own round; lower-priority items can batch together in a single cleanup session.

---

## Status as of handoff

**Shipped in Rounds 1–3 + doc-sync:**
- 60+ finding audit (`audit-findings.md`)
- 13 targeted fixes across UX, reliability, and consistency
- Unified `BacktestEngine` replacing three drifted implementations
- 24 unit tests as a permanent regression safety net
- Docs synchronized to shipped code
- `CHANGELOG.md` documenting user-visible changes

**Known limitations carried forward:**
- Daily Report timing is a 25–55 min placeholder, not a measurement
- Pro (v3) model availability is not surfaced in the UI when LightGBM is missing
- SEC EDGAR fetching has no retry logic or proper rate-limit handling
- yfinance empty-response case is not retried
- `fundamentals.db` size may need a ballpark refresh next quarter

---

## Priority 1 — Real correctness and reliability

These affect whether users can trust the numbers Quantfolio produces.

### Round 4 — Reliable data fetching (C-4 + C-5)

**Why it matters:** On a flaky Yahoo or SEC day, tickers currently drop silently from scans. Users see "no data available" for a ticker that is actually healthy but temporarily rate-limited. This undermines every downstream result.

**Scope:**
- Extract a shared HTTP client with exponential backoff, `Retry-After` respect, and a token-bucket rate limiter sized to documented budgets (10 req/sec for SEC)
- Replace `edgar_fetcher.http_get_json` one-shot `urlopen` with retried calls
- Treat yfinance empty DataFrames as retryable, not terminal
- Surface rate-limit warnings in scan logs so users see when data was skipped

**Suggested team:** skipper writes, wright reviews design, sophia reviews user-visible error surfaces.

**Estimated effort:** 1–2 hours.

**Test gate:** mock a 429 response and an empty DataFrame response. Verify retries happen, warnings surface, and no ticker is silently dropped.

---

### Round 5 — Stale prediction detection (C-9)

**Why it matters:** When today's feature row contains NaN, `predict_ticker` silently falls back to yesterday's features and returns a confident-looking prediction. Users cannot tell a fresh prediction from a stale one.

**Scope:**
- Detect the fallback condition in `finance_model_v2.py:460-522`
- Attach `warnings: ["stale_features_used"]` to the response
- Surface a muted banner in Ticker Lookup when this warning is present

**Suggested team:** skipper + sophia (no wright needed — small, localized).

**Estimated effort:** 30 minutes.

---

### Round 6 — Pro model availability surfacing (H-3)

**Why it matters:** When LightGBM is missing, Pro silently shows "Not available" with no indication the user needs to install something. Headline product differentiation becomes invisible.

**Scope:**
- On server startup, detect `HAS_LGBM=False`
- Surface a persistent banner in the dashboard header: "Pro model unavailable — install `lightgbm` to enable"
- Banner dismissible but reappears on next server restart

**Suggested team:** single agent, sophia preferred.

**Estimated effort:** 30 minutes.

---

## Priority 2 — UX cleanup round (batched)

A single cleanup session covering the remaining Round 1 High-priority and Nice-to-have findings. Most are individually small; the value is in doing them together.

### Round 7 — Batched UX cleanup

**Scope** (from audit-findings.md):
- **H-6:** cache `_load_library_summary` at process lifetime, invalidate on file mtime change
- **H-7:** add ARIA attributes (`role="tablist"`, `aria-selected`, `aria-sort`) and non-color signal distinction (icons or shapes for BUY/SELL/HOLD)
- **H-8:** delete dead `runPredict`, `setModel`, `#modelToggle` code from `frontend/index.html`
- **H-9:** replace empty `.catch()` on Ticker Lookup verdict fetch with a visible "Fundamental data unavailable — retry" row
- **H-13:** add ETA to Strategy Lab batch progress (elapsed / completed × remaining)
- **H-14:** `start_dashboard.bat` — detect non-conda Python installation, print clear message instead of silent activation failure; poll port 8000 before opening browser
- **H-16:** extend filter chip counts to VERDICT and ARCHETYPE chips in Leader Detector
- **H-17:** disable Download CSV button when zero rows selected; replace `alert()` with tooltip
- **N-1 through N-14:** the 14 smaller Nice-to-have items (dead code removal, logging improvements, minor UX polish)

**Suggested team:** skipper + sophia, one commit per item. Wright not needed — nothing architectural.

**Estimated effort:** 2–3 hours.

**Warning:** UI changes need manual verification. Round 2 taught us that agent-reviewed UI code ships regressions. Plan to click through every changed tab before merge.

---

## Priority 3 — Measurement and calibration

Things only real use can produce.

### Ongoing — Daily Report timing

Next time you actually run a full Daily Report end-to-end, time it with your phone. Update:
- `frontend/index.html` — `DAILY_REPORT_EST` constant
- `api_server.py:587` — the timing banner
- `USER_GUIDE.md` Parts 4 and 11

Three-line fix, one commit, no agent required.

---

## Priority 4 — Big unexamined questions

These are the ambitious asks. Worth considering carefully whether and when to engage agents here.

### ML model correctness (explicitly out of scope in Rounds 1–3)

Every prior round ruled this out: "do not evaluate trading strategies themselves, statistical soundness of models, or whether predictions are financially meaningful."

If you want agents to engage here, the framing matters:
- Agents can **critique methodology** — look-ahead bias, data leakage, improper train/test splits, survivorship bias in the universe
- Agents can **propose additional backtests** — out-of-sample periods, sector-specific performance, performance in different volatility regimes
- Agents should **not** make financial claims or recommendations — that's your call
- Agents should **not** validate whether strategies are profitable — backtest results are not predictions

Treat this as a research assistant round, not an engineering round.

**Estimated effort if pursued:** multi-session, 4+ hours.

---

### Security audit

Nobody has looked at Quantfolio from a security angle. Areas to examine:
- Input validation on ticker symbols (SQL injection, path traversal)
- CSRF on the rebuild endpoint
- Authentication/authorization on scheduled jobs
- Secrets handling in `.env`
- Dependency vulnerability scan (now feasible because dependencies are pinned)

**Suggested approach:** a dedicated "security-reviewer" subagent, read-only audit similar to Round 1.

**Estimated effort:** 2 hours for audit, plus follow-up rounds for any critical findings.

---

### New features from your usage

Keep a running list as you use Quantfolio over the next few days. When you have 5–10 items, we shape them into a scope and run a feature round.

Common feature categories worth considering:
- Export/sharing (CSV, PDF, shareable URLs)
- Saved comparisons (strategy A vs B across tickers)
- Portfolio-level analysis (not just per-ticker)
- Alert customization (thresholds, schedules, channels)
- Historical performance tracking (how have past predictions performed?)

---

## Recommended order of operations

When you come back:

1. **Update timing first.** Easiest commit, fixes a known stale number.
2. **Run Round 4 (reliable data fetching).** Highest correctness impact remaining.
3. **Run Round 5 and 6 back-to-back.** Both small, both fill real product-integrity gaps.
4. **Pause and use Quantfolio for a week.** Collect real-world feedback.
5. **Run Round 7 (UX cleanup).** Batch everything remaining from the audit.
6. **Decide on Priority 4 items.** ML, security, or features — one at a time.

---

## Notes on agent workflow

Things learned from Rounds 1–3 worth carrying forward:

- **Three-agent teams are overkill for small, focused work.** Doc sync and Rounds 5/6 are fine with a single agent.
- **Test-first is non-negotiable for correctness refactors.** Baseline capture → engine extraction → per-caller verification is the shape that saves you.
- **Manual verification is required for anything UI or Windows-specific.** Agents cannot click a button or run a batch file in a way that catches real regressions. Round 2 shipped two regressions that would have been caught in 5 minutes of human clicking.
- **Atomic commits per finding** beat bucketed commits for any work that might need revert. Bucketed commits are fine when changes are tightly coupled (Round 2 Bucket 2 and 4).
- **Accept-edits mode is the right default.** Auto mode is too permissive for real production work. Default mode is too noisy once you've approved common patterns.
- **`.claude/settings.local.json` in `.gitignore`** prevents personal permissions from polluting the repo.

---

## Reference — where things live

- `audit-findings.md` — Round 1 findings
- `round2-summary.md` — Round 2 work summary
- `round3-summary.md` — Round 3 work summary (with 7-ticker verification)
- `CHANGELOG.md` — user-facing release notes
- `tests/backtest_baselines/` — golden reference files for backtest comparisons
- `.claude/agents/` — Wright, Skipper, Sophia definitions (travel with repo)

---

Written with care after a three-day collaboration. Future you: you've got a good tool, real documentation, and a tested workflow. Pick up whenever you're ready.
