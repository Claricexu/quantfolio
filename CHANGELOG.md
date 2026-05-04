
## 2026-05-15 — Round May 15 (Forensic Flags)

- New **forensic-flag layer** alongside the verdict — a separate signal that warns about hidden accounting fragility on otherwise-good firms without modifying the LEADER/WATCH/AVOID/INSUFFICIENT_DATA verdict (Round 9a's "verdicts encode pure quality" invariant is preserved). Three of the four originally-scoped flags ship working:
  - `ni_ocf_divergence` — Reported NetIncome > OperatingCashFlow for 3 consecutive fiscal years. Sometimes a sign of aggressive accounting; banks/insurers/REITs (SIC 6000-6799) are excluded because the pattern is normal under their accounting frameworks.
  - `leverage_high` — Net Debt / EBITDA > 4x AND interest coverage < 2x. Both legs required (AND threshold).
  - `dilution_velocity` — Single-year shares-outstanding YoY growth > 10%. Complements the existing `flag_diluting` dealbreaker (which checks 15% over 3 years and can miss a one-year burst).
- **Going-concern detection deferred.** 3-of-4 forensic flags shipped; going-concern detection requires text-parsing infrastructure not currently in scope. Empirical investigation against nine known going-concern filers found no XBRL exposure for `SubstantialDoubtAboutGoingConcern`; SEC files this signal as narrative text in 10-K Item 8 (Auditor's Report). The schema slot is wired in (column ships, chip label exists, override CSV accepts `flag_name=going_concern`) so a future 10-K text-parser drops in without any consumer changes.
- **Option β chosen** for the integration shape: forensic flags are a separate column family on `screener_results.csv` (`forensic_flags_json` + `forensic_flag_count`), not a modifier on the existing verdict. This preserves Round 9a's invariant that verdicts encode pure quality and keeps the two signals independently inspectable on the row.
- **Override infrastructure** at `cache/forensic_flag_overrides.csv` — schema `symbol,flag_name,expires_at,reason`, lazy-loaded once per process, suppressed flags stay True in `forensic_flags_json` (so the UI can render an "overridden" chip with dashed border + muted text) but are excluded from `forensic_flag_count`. Lets the operator recover from a known false positive without modifying detection logic and without permanently silencing the flag for the symbol.
- **Leader pool tightening:** `leader_selector.py` now drops any LEADER row with `forensic_flag_count > 0` from `leaders.csv`. Legacy rows from a pre-Round-May-15 `screener_results.csv` (no `forensic_flag_count` cell) pass through as if the count were zero — graceful degradation. Under-fill log now distinguishes forensic-driven shrinkage from genuinely-too-few-LEADERs.
- **Frontend:** amber forensic-flag chips render on the Verdict Card alongside the existing red dealbreaker chips (reds first, ambers second). A one-line dismissible legend strip above the Leader Detector table calls out the red-vs-amber semantic ("excludes from leader pool" vs. "accounting concerns, doesn't change verdict"). Override-suppressed flags render at 0.55 opacity with a dashed border and a "· muted" suffix on the chip label.
- 37 new unit tests under `tests/unit/test_forensic_flags.py` (27) and `tests/unit/test_leader_selector.py` (10) covering the four flags, sector exclusion, override CSV parsing, and pool-filter integration. All pin behaviours that would otherwise drift silently.

## 2026-05-03 — Round 9a

- Verdict consolidation: the previous five-tier `LEADER / GEM / WATCH / AVOID / INSUFFICIENT_DATA` schema collapses to four tiers — `LEADER / WATCH / AVOID / INSUFFICIENT_DATA`. The Phase-1.9 `LEADER` vs. `GEM` split was a sector-rank tiebreak at 5/5 (rank ≤ 5 → LEADER, rank > 5 → GEM); both meant "passes all five archetype-tuned business-quality tests with no dealbreaker," differing only in company size. Encoding size into a quality verdict label was confusing — a great small-cap and a great mega-cap both deserve the same quality tag — so the verdict is now size-blind. Sector rank still rides on the row via `market_cap_rank_in_sector` (used by the moat-fallback test and surfaced as the Leader Detector's SECTOR RANK column), it just no longer steers the verdict.
- `leader_selector.py` simplified accordingly: `leaders.csv` is now the top-100 LEADER rows by `good_firm_score` (tie-break: market_cap desc). Pre-9a behaviour reached into GEM to fill the 100-slot cap; with GEM gone there is no second pool. Under-fill is now possible and is surfaced via a `[note]` line in the build output — the selector intentionally does not reach into WATCH (which represents 3–4/5 tests passed, a different quality tier).
- Score formula now correctly capped at its real maximum of 95. The previous `min(score, 100)` cap in `fundamental_screener.score_ticker` was dead code (5 × 15 + 10 + 5 + 5 = 95).
- Frontend: Leader Detector's verdict filter chips drop the **Gem** chip; verdict colour helpers fall pre-9a `GEM` payloads back to the LEADER colour family so any cached older JSON still renders coherently. The dealbreaker chip on the verdict card now labels `cagr_shrinking` as **Shrinking Revenue** instead of rendering the literal field name.
- Documentation: `good_firm_framework.md` now documents the actual `good_firm_score` formula, corrects the `burning_cash` description (single-period TTM `ocf_ttm < 0`, not the FCF-plus-runway formulation an earlier draft described — the simple TTM check is intentional given semi-annual SEC filing cadence), softens the "failing any one is a dealbreaker" framing that contradicted WATCH-tier routing, and annotates the *Instant Dealbreakers* table as aspirational (none of those patterns are automatically detected in the shipped screener; closing those gaps is a separate methodology project queued ahead of the 5/15 SEC fetch). README and USER_GUIDE updated to the four-tier verdict throughout.

## 2026-05-02 — Round 8d

- Biweekly backtest auto-refresh: every other Friday at 9 PM ET, the server sweeps your library and re-runs any ticker whose cached backtest is at least 15 days old. Keeps the Strategy Lab fresh without burning a manual "Run All Backtests" job.
- Backtest cache TTL bumped from 7 days to 15 days (`BACKTEST_CACHE_TTL_DAYS`). The "Run All Backtests" button and the new biweekly cron both use the longer freshness window.
- Case B (insufficient-data) tickers — names that fail to backtest because they lack enough history — are now classified separately and deferred for 8 weeks before retry, so a brand-new IPO doesn't get re-attempted every other Friday for nothing.
- New endpoint: `GET /api/backtest-refresh/status` exposes last run, next run, and the Case B history for visibility into the scheduler.
- Refresh state persists to `cache/last_backtest_refresh.json` so a server restart doesn't lose the parity anchor or the Case B deferrals.

## 2026-05-02 — Round 8c

- Daily Report tab now has a **Send Email Alert** button that re-fires the Signal Brief from the most recent cached report — handy if SMTP was misconfigured during the scheduled 4:05 PM run, or if you want to forward a copy to a new recipient without waiting for tomorrow.
- Confirmation dialog before sending shows the recipient count and the current BUY/SELL counts, so the manual trigger isn't a single accidental click away from blasting the list.
- New endpoint: `POST /api/alerts/send-manual` re-sends the brief from the cached daily report; it shares the same `_classify_alert` rule path as the scheduled cron, so manual and automatic emails always agree on which rows qualify.
- Companion endpoint `GET /api/alerts/config` exposes the SMTP-enabled flag and the recipient count for the confirmation dialog.

## 2026-05-01 — Round 8b

- Email alert rules refined with backtest-validated single-model paths: a Pro-only BUY now fires when the ticker's `best_strategy` is `pro_buyonly` or `pro_full`; a Lite-only BUY fires when best is `lite_buyonly` or `lite_full`. Consensus BUY (both models) still fires unconditionally.
- SELL gate hardened: SELLs are suppressed entirely when `best_strategy` is `buyhold`, `lite_buyonly`, or `pro_buyonly`. SELL fires only on `pro_full` + Pro=SELL or `lite_full` + Lite=SELL. Conflicts (one model BUY, the other SELL) never fire.
- Email alerts now include a Peer SVR column showing the industry-group median SVR alongside each ticker's own SVR — quick valuation context per row.
- Daily Report's All Symbols Consensus column now uses the same `classifyAlertRow` predicate as the email, so the dashboard and the email never disagree on what "qualifying" means.

## 2026-04-30 — Round 8a

- SVR card on Ticker Lookup restructured into a 3-line layout: ratio with inline qualifier, peer median row, color-coded valuation tier. The verdict card's old SVR row was removed (the Lookup card is now the canonical SVR surface). Iterated three times to land the final vertical balance.
- All four naive-timestamp write sites in the report and verdict pipeline now write `datetime.now(ZoneInfo('America/New_York'))` — the latent non-EST deployment bug flagged in DEVELOPMENT.md is closed. A back-compat shim localizes any pre-upgrade naive ISO loaded from disk.
- ET marker added to user-facing time-of-day displays (Daily Report banner, scheduler hints) for timezone clarity.
- Accessibility bundle: `aria-pressed` on filter chips, `aria-live` on load surfaces, `prefers-reduced-motion` honored, Escape closes inline detail panes, WCAG AA contrast bump on remaining 10–13px chrome.

## 2026-04-28 — Round 7d

- Verdict card now shows a peer-median column alongside each company value — for the 8 metrics where it makes sense (Revenue YoY, Revenue 3Y CAGR, Gross Margin, Operating Margin, FCF Margin, Rule of 40, ROIC, SVR). Peers are bucketed by industry group; em-dash renders when fewer than 5 industry-group peers report the metric. Categorical rows (Sector, Industry Group, Industry, Sector Rank) render em-dash in the peer column to keep visual rhythm.
- Verdict card now carries a small "as of" timestamp chip beside the SCORE box, showing when the underlying screener data was last refreshed. Hover reveals the raw timestamp for debugging stale-cache complaints. Style matches the existing Daily Report and Leader Detector freshness chips.
- Compare card P/E value now shows a "Weighted average of holdings" tooltip on hover when the ticker is an ETF — clarifies that an ETF's P/E is a holdings-weighted aggregate, not a valuation signal in the usual sense.
- ETF verdict cards now show an inline note "Peer median comparison not applicable for ETFs." below the metric grid, instead of an unexplained column of em-dashes.
- Behind the scenes: dropped the old `svr_vs_sector_median` field and its `+5` Good Firm Score bonus — peer-median SVR (industry-group bucket) supersedes the SIC-2 ratio. Maximum ±5 score drift per ticker; rebuild `leaders.csv` to see the new ranking. New `peer_count` column carried in the CSV for forward-compat ("n=12" tooltip in a future round).

## 2026-04-27 — Round 7c-2

- Replaced redundant Sector card on Ticker Lookup with a P/E (Price-to-Earnings) card. Sector and Industry information remain available on the Verdict Card below. P/E shows trailing P/E ratio with em-dash fallback for negative earnings or missing data.

## 2026-04-26 — Round 7c

- Canonical classifier module (classifier.py): SIC ranges + 9 ticker overrides → (sector, industry_group, industry); replaces the JS-side `broadSector` derivation.
- Industry Group filter chips on Leader Detector with cross-narrowing against the Sector dropdown.
- Source-rename arc replacing ad-hoc sector strings with classifier outputs across screener and frontend.

## 2026-04-25 — Round 7b

- FB-6: Strategy Lab defaults to the symbols in the most recent Daily Report; "Show all symbols" toggle bypasses (does not persist across reloads).
- FB-7: Banner-level "Close prices as of …" aggregation on Daily Report; per-row "As of" column and Firm-Score-header chip removed in favor of the banner.

## 2026-04-25 — Round 7a

- Verdict card now expands inline beneath the clicked ticker row on Daily Report and Leader Detector, instead of always showing at the top of the tab. Single card open at a time; close with × or by clicking another ticker.
- Strategy Lab comparison chart expands inline beneath the clicked library row, with the same close-on-× behavior.
- Ticker Lookup verdict cache now stays valid through weekends (Friday's report remains acceptable until Monday's scheduled run), and the cache loads from disk on first access after a server restart.
- Leader Detector freeze fix: avoid the multi-second browser hang when expanding rows on the Universe table.

## 2026-04-24 — Round 5 + 6

- Ticker Lookup flags predictions built from stale features when today's data is incomplete.
- Dashboard shows a one-line banner on first load when the Pro model is unavailable, with install instructions.
- Internal: new `/api/system/status` endpoint exposes runtime capability checks.
- Internal: removed dead single-model-toggle code from the frontend.

# Changelog

User-visible changes to Quantfolio. Engineering-side commentary lives in `round2-summary.md` and `round3-summary.md`; this file is the short, readable story.

The changelog is organized by release round. Each round closed a batch of audit findings from `audit-findings.md`; the round summaries name the IDs if you want to trace a line item back to its original audit entry.

---

## Round 4 — 2026-04-23

A reliability pass for the two external data feeds (Yahoo Finance and SEC EDGAR).

### Changed

- **Data fetches now retry on rate limits instead of silently dropping tickers.** On a flaky Yahoo or SEC day, tickers used to vanish from the Daily Report with no explanation — the underlying request got throttled and the app moved on. Both feeds now retry with exponential backoff (and, for SEC, honor the server's `Retry-After` hint when present), and only give up after several attempts. When a batch really does exhaust its retry budget, it now raises a visible error instead of quietly returning nothing — so a real upstream outage looks different from a clean "no hits today."

---

## Round 3 — 2026-04-23

One focused refactor: same backtest path for everyone.

### Changed

- **Unified backtest engine.** Before Round 3, the dashboard, the CLI backtest, and the same-day prediction each ran their own walk-forward loop. For a handful of tickers (MSFT was the canonical case) that meant the dashboard and the CLI would quote different Sharpe ratios for the same symbol. All three call sites now route through a single `BacktestEngine` with one config, one ensemble builder (OOF stacking), and one config-hashed result. The numbers you see in Ticker Lookup, the Strategy Lab library, and the CLI now always agree for a given ticker and data snapshot.
- **Short-history tickers are safer.** The `MIN_ZSCORE_SAMPLES=20` floor is now enforced in every backtest path, including the one-shot prediction path in Ticker Lookup. Previously, short-history tickers (think recently-IPO'd names with < ~120 trading days of data) could slip below the floor on the prediction path while the backtest path enforced it — producing a Z-score that looked confident but was built from too few samples. Now they are treated uniformly: the engine HOLDs until the sample count clears the floor.

### Internal

- First unit-test suite landed at `tests/unit/` (24 tests, runs via `python tests/unit/run_all.py`). Focus is the new `BacktestEngine`; regression tests for the screener rubric and feature engineering are still open.
- The legacy "fast" val-MAE ensemble builder was removed. Only OOF stacking is supported — slower, but the path-divergence bug was worth the cost.

---

## Round 2 — 2026-04-21

The trust-and-UX pass: making sure the four tabs agree with each other and that long-running actions don't lie.

### Changed

- **One source of truth for fundamental verdicts.** Ticker Lookup's verdict card, Daily Report's Firm Score column, and the Leader Detector table now all read from the same screener output. A ticker that is a LEADER on one tab will be a LEADER on every tab in the same session, and the verdict surface shows a small "as of HH:MM" chip so you can tell when the underlying screen was last run.
- **Better "why is this INSUFFICIENT_DATA" messaging.** Tickers that the SEC taxonomy simply can't resolve (CRWD, VLO, APA, FSLY have been the repeat offenders) now report a distinct `TAXONOMY_GAP` reason instead of the generic "no SEC data" hint. The verdict card renders the specific reason text rather than guessing.
- **The 174-symbol scan takes 25-55 minutes, not 2-5.** The earlier timing claim was wildly off; both the dashboard banner and the user guide now cite the realistic range. (This is still a conservative guess — see the Round 2 summary for the verification gap.)
- **High-Confidence SELL filter matches the email.** The dashboard's SELL list now applies the same "only full-signal strategies" filter that the 4:05 PM email has always used. Rows that used to appear as confusing SELLs on buy-and-hold names are now downgraded to HOLD with a small info icon explaining why.
- **Rebuild Now is no longer one click away from a 3.5-hour job.** Clicking Rebuild Now opens an inline confirmation modal that requires you to type the word `REBUILD` (case-insensitive) before the Start button activates. The modal also shows the estimated duration (cold vs warm) and the last-rebuild timestamp.
- **Long-running fetches stop timing out at 60 seconds.** Daily Report generation and batch backtests can legitimately run for minutes; previously the frontend would silently abort the request at the 60-second mark. Polling paths now run without that timer and surface a "still working, 42s elapsed" status line so the UI stays honest about what the server is doing.
- **Library rows are discoverable as clickable.** The Strategy Lab library used to hide its equity-curve drill-down behind an un-signalled click target on each row; now the row has a pointer cursor and a micro-hint above the table.
- **Accurate ticker count in the footer.** Was hardcoded at "78+"; now pulled live from `/api/symbols` (currently 174) with a safe fallback.

### Added

- `requirements.lock` — checked-in `pip freeze` snapshot for byte-reproducible installs. `requirements.txt` is now pinned to exact versions rather than `>=` lower bounds.
- Startup dependency probe in `start_dashboard.bat` now imports the full top-level set (`fastapi, uvicorn, pandas, numpy, sklearn, yfinance, xgboost, lightgbm, apscheduler`) and prints a clear install-or-skip message for each missing package — including a "Lite still works without LightGBM" hint so a user missing Pro doesn't panic.
- Skeleton empty states on Daily Report, Strategy Lab, and Leader Detector — first-time users see what each tab will eventually contain rather than a blank banner.

### Fixed

- `fundamentals.db` is now opened in WAL mode with a 30-second busy timeout. A manual refresh no longer locks out concurrent dashboard reads.
- Daily Report's "Generated: Invalid Date" banner is gone — when the cached report lacks an explicit `generated_at` field, the timestamp is derived from the file's mtime.

---

## Round 1 — 2026-04-20 and earlier

The baseline the audit was run against. Highlights that end users saw, compiled from the `main` history:

- Moved SMTP and SEC credentials into a gitignored `.env` file, with `.env.example` checked in as a template. Email alerts and the SEC EDGAR contact header both read from there.
- Fixed several verdict-card UI bugs in the Ticker Lookup tab.
- Added the Leader Detector tab and the Layer 1 pipeline (universe builder → SEC XBRL fetch → archetype-routed screener → leader selection) that feeds it.

See the repository commit log prior to 2026-04-21 for the full Round 1 history.
