# Quantfolio — Iteration Plan V2 (Refreshed)

**Scope:** Rounds 7a through 7d — UX refinement and classification correctness based on owner feedback after Round 6.

**Starting point:** main at the post-Round-6 merge.

**Last refreshed:** 2026-04-27 (after Round 7c-2 merge). Original plan was written 2026-04-24; this revision marks shipped rounds and refines remaining scope.

---

## Iteration shape

Originally planned as four rounds (7a, 7b, 7c, 7d). Reality shipped as five rounds (7a, 7b, 7c, 7c-2, with 7d still pending) because Round 7c surfaced a UX redundancy (the Sector card on Ticker Lookup) that warranted its own focused round.

| Round | Scope | Status | Outcome |
|---|---|---|---|
| 7a | Inline expansion (FB-2, FB-8) + backend cache fix | Shipped 2026-04-25 | 13+ commits across 6 verification rounds |
| 7b | Daily Report polish (FB-6, FB-7) | Shipped 2026-04-25 | 5 commits, one regression patch |
| 7c | Classifier module + Industry Group filter (FB-1 data, FB-5) | Shipped 2026-04-27 | 11+ commits across multiple sessions; included source-fix arc |
| 7c-2 | Sector → P/E card swap | Shipped 2026-04-27 | 5 commits, one verification round |
| 7d | Peer benchmarking + timestamp + layout refactor (FB-1 display, FB-4) | Pending | 3-4 hours estimated, three-agent team |

---

## Round 7a — Inline expansion + backend cache fix [SHIPPED]

**What shipped:**
- Verdict card expands inline beneath clicked row on Daily Report and Leader Detector (FB-2). One card open at a time. Auto-scroll into view. Modal fallback below 640px.
- Strategy comparison chart expands inline beneath clicked library row on Strategy Lab (FB-8). Same one-open pattern.
- Six verification rounds patched: card clipping (sticky positioning fix), cache for re-clicks, missing × button on Daily Report, loading skeleton, Leader Detector close-path layout-flush freeze, and the unrelated weekend-aware cache freshness fix.
- Backend: weekend-aware cache freshness via `_is_cached_report_acceptable` and `_scheduled_run_occurred_between` helpers. Cold-start fallback to disk via `_load_latest_report_from_disk`. Friday's report stays valid through Monday 4:05pm.

**Lessons captured:**
- PATTERNS.md P-2 — avoid synchronous layout flushes after mutating large tables (1,414 rows × 9 columns = 12,700 cells; each focus()/scrollIntoView() call forces full re-measure).
- All-surfaces verification: the × button missing-on-Daily-Report bug taught us to enumerate UI surfaces explicitly.

---

## Round 7b — Daily Report polish [SHIPPED]

**What shipped:**
- Strategy Lab defaults to Daily Report symbols (FB-6). Override toggle for showing all symbols. Session-only, not persisted across reloads.
- Daily Report banner aggregates per-date close prices across symbols (FB-7). Multi-date case shows breakdown; single-date case shows unified text.
- Per-row "As of" column removed from all three Daily Report tables (HIGH-CONFIDENCE BUY, HIGH-CONFIDENCE SELL, ALL SYMBOLS).

**Lessons captured:**
- PATTERNS.md P-3 — single-line PowerShell git commit messages on Windows. Multi-line here-strings break shell quoting in subtle ways.
- PATTERNS.md P-4 — trace caller chains, not just function bodies. The As-of column removal initially patched only the row renderer, not the shared `buildReportTable` header construction that decorates the column for all three tables.

---

## Round 7c — Classifier module + Industry Group filter [SHIPPED]

**What shipped:**
- `classifier.py` — pure Python module. Function `classify(symbol, sic, sic_description) → (sector, industry_group, industry)`. Hand-crafted `TICKER_OVERRIDES` for 9 mega-caps (GOOGL, GOOG, META, NFLX, AMZN, AAPL, TSLA, V, MA). 10 sectors. 29 industry groups (originally 30; "Interactive Media & Services" merged into "Telecom & Media" so all groups have ≥5 members for peer math). 9 unit tests.
- Source-fix in `fundamental_metrics.py:486` — renamed `m['sector'] = info.get('sic_description')` to `m['sic_description']`. Removed the upstream mislabeling that had been silently corrected downstream by `fundamental_screener.py`. Cleaner data semantics throughout.
- `fundamental_screener.py` — calls `classify()`, writes `sector / industry_group / industry / sic_description` columns to `screener_results.csv`.
- `verdict_provider.py` — surfaces classifier fields in verdict response.
- `api_server.py` — `_inject_classifier_fields` overlays canonical values on `/api/predict` and `/api/predict-compare` responses (the parallel pipeline).
- Frontend: verdict card and Leader Detector Sector column read canonical fields. JS-side `broadSector(sic, ...)` retained as legacy-CSV fallback only.
- Industry Group filter chips on Leader Detector below Sector dropdown (FB-5). AND-combined with Sector filter. Chip counts update with filter context.
- Industry tier returns `sic_description` for non-override tickers; falls back to `industry_group` when description missing. Override tickers continue to use hand-crafted industry value.

**Lessons captured:**
- Architecture pattern: parallel pipelines silently bypass new integrations (`/api/predict-compare` didn't go through `verdict_provider`; frontend `broadSector` competed with Python classifier). Future integrations need to enumerate all parallel pipelines explicitly.
- Spot-check methodology: pick tickers whose classification differs across systems to expose integration gaps. Override-only tests miss non-override regressions.
- Deferred timezone bug: `_run_dual_report` writes naive timestamps; the cache-freshness helper assumes naive = America/New_York. Correct on the owner's EST Windows machine; must fix before any non-EST deployment. 4-line fix in two write sites.

---

## Round 7c-2 — Sector → P/E card swap [SHIPPED]

**What shipped:**
- Backend: `pe_trailing` column added through fundamental_metrics → fundamental_screener → screener_results.csv → API responses.
- Frontend: Sector card on Ticker Lookup (the 4th card in the SVR / Market Cap / Quarterly Revenue / Sector row) replaced with a P/E card. The Verdict Card below provides Sector / Industry Group / Industry, making the standalone Sector card redundant.
- Display: P/E formatted as `{value.toFixed(1)}x` for values < 100, `{Math.round(value)}x` for ≥ 100. Em-dash fallback for negative earnings, missing data, ETFs without P/E, and pathological values.
- Touchups during the round (sophia review): P/E font size set to 15px (peer with Market Cap and Quarterly Revenue, not SVR's 20px headline weight). SVR card renders at full size with em-dash when N/A, matching the populated-state visual weight.

**Lessons captured:**
- "Patching code that's about to be deleted is wasted effort" — the redundant Sector card had a Yahoo-vs-classifier inconsistency we initially planned to patch, then realized deletion was the right fix instead. Documented as a known issue in summary, then resolved through the swap.

**Deferred to Round 7d:**
- ETF P/E tooltip — sophia recommended `title="Weighted average of holdings"` on the P/E value when isETF. Acceptable to defer; will be addressed alongside the verdict card layout refactor in 7d.

---

## Round 7d — Peer benchmarking + timestamp chip + layout refactor [PENDING]

**Goal:** Turn the verdict card from a data display into a comparison tool. Each metric shown alongside its industry-group peer median, with a clear "as of" timestamp.

**Three-agent team likely:** wright reviews architecture (where peer-median computation lives), skipper implements, sophia reviews visual hierarchy.

**Estimated effort:** 3-4 hours of agent work. One or two verification cycles.

### Scope

**A. Peer benchmarking column on the verdict card.**

For each metric in the verdict card (Revenue YoY, Revenue 3Y CAGR, Gross Margin TTM, Operating Margin, ROIC TTM, Rule of 40, FCF Margin), display the company's value alongside the peer median for its industry group. Visual: progress bar or percentile indicator showing where this company sits in its peer distribution.

Architectural decision required: peer median computed server-side at API response time (cached per industry group), or client-side from the screener data already in the browser. Wright should weigh tradeoffs in Phase 1.

**B. Timestamp chip on the verdict card.**

Small "As of YYYY-MM-DD" indicator showing when the underlying data was last refreshed (i.e., when the screener last ran for this ticker). Helps users distinguish stale data from fresh.

**C. Verdict card layout refactor.**

Sophia flagged in Round 7c that the current verdict card uses two-column flex (label | value). Adding the peer-median column requires three columns (label | company value | peer median). For rows where peer median doesn't apply (Sector, Industry Group, Industry), the third column shows em-dash without alignment collapse. CSS Grid handles this cleanly; flex doesn't.

**D. Rolled-in: ETF P/E tooltip from Round 7c-2.**

Add `title="Weighted average of holdings"` to the P/E value on the four-card row when the ticker is an ETF. Small frontend addition during the layout work.

### Things to decide in the prompt

- **Peer median computation location:** server-side caching vs client-side aggregation. Server-side is simpler API design, client-side is faster for filter changes. Wright reviews tradeoffs.
- **Visual treatment of peer comparison:** raw values side-by-side, percentile rank, progress bar with median marker, or some combination. Sophia reviews visual hierarchy.
- **What metrics show peer comparison vs not:** Sector / Industry Group / Industry are categorical (no peer median possible). Numeric metrics get peer columns. Edge cases: Rule of 40 (composite score), Growth Trajectory (categorical text). Decide ahead of implementation.
- **Layout refactor scope:** two-column flex → three-column grid is the structural change. Should existing verdict card metrics keep their current visual style, or get a coordinated polish? Bias toward minimal: only change what the peer column requires.
- **ETF handling for peer comparison:** ETFs don't have meaningful peer groups in our taxonomy. Either show em-dash in the peer column for ETFs, or skip the peer column entirely for ETFs. Probably em-dash for visual consistency with non-ETF cases where peer median is missing.

### Constraints (lessons from earlier rounds)

- Reference PATTERNS.md P-1, P-2, P-3, P-4 in the prompt.
- Verify data shape with `Get-Content -TotalCount 1` before assuming column existence.
- Spot-check both override and non-override tickers; verify both Verdict Card and Sector Card surfaces; verify Daily Report and Leader Detector inline expansion still works.
- Single-line PowerShell commit messages.
- Test-first for any new computation (peer median aggregation deserves unit tests).

### Verification gates

- Existing 51 tests still pass.
- New peer-median computation has unit tests (likely 4-6 tests covering: median of group, missing-data handling, ETF case, sector mismatch).
- Manual UI spot check across 4-6 ticker types: profitable non-override (NVDA), profitable override (GOOGL), loss-making (RIVN), high-percentile-rank ticker (test "you're in the top 10% for ROIC" reads correctly), ETF (SPY).

---

## Out of scope for this iteration

These items are tracked for future rounds but not part of 7a-7d:

- **Polish/a11y consolidation round** — ~30 deferred items from rounds 7a-7c-2 (aria attributes, prefers-reduced-motion, mobile chip-row, etc.). Should ship as a batch in a dedicated round.
- **Latent timezone bug** — `_run_dual_report` writes naive timestamps. 4-line fix in two write sites. Defer until non-EST deployment is contemplated.
- **H-2** through **H-17** items from the original Round 1 audit (`audit-findings.md`) — most still open. Address in a future audit-followup round.
- **ML methodology review** — separate iteration entirely. Not a round, a project.
- **Security audit** — separate iteration. Not a round, a project.

---

## Process notes (lessons across the iteration)

- Verification rounds are non-negotiable. Rounds 7a-7c-2 averaged 2-4 verification cycles per round; trying to skip them ships regressions.
- Owner spot-checks find bugs that automated tests can't (CSS rendering, parallel pipelines, classifier override propagation). Format: structured table with red-highlighted failures.
- Three-agent team for architectural rounds; single-agent for small focused rounds. Match team size to scope.
- Test-first for new modules. Tests committed in the same commit as the module, not after.
- Source fixes beat downstream patches when both are feasible. Round 7c's `fundamental_metrics.py:486` rename cleaned up years of accumulated downstream-correction patches.
- Manual CSV regeneration after pipeline changes is an owner step, not an agent step. Documented in each round's verification checklist.

---

Last refreshed 2026-04-27 after Round 7c-2 merge. Round 7d is the final round in this iteration. After 7d ships, the iteration closes and a new plan should be drafted (likely focused on the polish/a11y consolidation round, then ML methodology review).
