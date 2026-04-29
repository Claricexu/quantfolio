
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
