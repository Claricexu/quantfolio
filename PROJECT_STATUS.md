# Quantfolio — Project Status

**Last updated:** 2026-04-27 (after Round 7c-2 merge)

## What Quantfolio is

A local-first, single-user equity research tool. Screens a ~1,400-ticker universe via SEC EDGAR fundamentals + Yahoo Finance pricing, runs two prediction models (Lite RF+XGBoost, Pro stacking ensemble with LightGBM), backtests strategies, and surfaces consensus signals. No cloud, no auth, runs entirely on the user's machine.

## What it does well right now

**Daily Report** — runs all symbols through both models, surfaces high-confidence BUY/SELL signals, schedules weekday email summaries at 4:05pm EST.

**Ticker Lookup** — single-symbol prediction with verdict card showing canonical Sector / Industry Group / Industry, plus four-card row (SVR / Market Cap / Quarterly Revenue / P/E).

**Strategy Lab** — backtest comparison across strategies, defaults to Daily Report symbols, inline chart expansion beneath clicked rows.

**Leader Detector** — filters the universe by canonical Sector and Industry Group, shows the four-tier verdict (LEADER / WATCH / AVOID / INSUFFICIENT_DATA), inline verdict cards on row click.

**Classifier** — pure Python module with 10 sectors, 29 industry groups, hand-crafted overrides for 9 mega-caps whose SIC codes misrepresent their actual business.

## What changed in the current iteration (Rounds 7a through 7c-2)

**Round 7a** — Inline expansion pattern. Verdict cards expand beneath clicked rows on Daily Report and Leader Detector. Strategy chart expands beneath clicked library row. Plus weekend-aware cache freshness fix (Friday's report stays valid through Monday 4:05pm) and cold-start cache fallback to disk.

**Round 7b** — Daily Report polish. Banner aggregates close-price dates across symbols. Strategy Lab defaults to Daily Report symbols with override toggle. Per-row "As of" column removed from all three Daily Report tables.

**Round 7c** — Classification system. New `classifier.py` module with canonical 10-sector / 29-industry-group taxonomy. Source-fix in `fundamental_metrics.py` removed an upstream mislabeling that had `sic_description` stored as `sector`. Verdict card and Leader Detector now show classifier-canonical values. Industry Group filter chips added to Leader Detector. Industry tier returns SIC description (e.g., "Semiconductors & Related Devices") for non-override tickers, hand-crafted value for override tickers.

**Round 7c-2** — Sector-to-P/E card swap. Replaced redundant Sector card on Ticker Lookup with a P/E card. Trailing P/E displayed at 15px (peer with Market Cap and Quarterly Revenue, not headline-weight like SVR). Negative earnings, missing data, and ETF-without-P/E all fall back to em-dash cleanly.

## What's not done yet

**Round 7d (planned)** — Peer benchmarking on the verdict card. Each metric (Revenue YoY, Operating Margin, ROIC, etc.) shown alongside its industry-group peer median. Plus a timestamp chip on the verdict card showing when the underlying data was last refreshed. Plus a CSS layout refactor (two-column flex → three-column grid) to accommodate the peer-median column without alignment collapse. Plus the deferred ETF P/E tooltip from Round 7c-2.

This is the round that turns the verdict card from a data display into a comparison tool ("how does this company stack up against its industry peers?").

## What's deferred to a future polish round

A consolidation round will eventually clean up ~30 deferred items accumulated across rounds 7a-7c-2:
- Accessibility: aria-pressed on filter chips, aria-live on loading skeleton, prefers-reduced-motion handling, focus management refinements.
- Mobile: chip-row collapse on narrow viewports, modal fallback documentation in USER_GUIDE.
- Polish: chip count parity under filter combinations, cache-rebuild interaction with verdict cache.
- Latent bugs: timezone naivety in `_run_dual_report` write paths (correct on EST machine, wrong elsewhere).

These items live in the per-round summary documents (`round7a-summary.md` through `round7c-2-summary.md`). A future "polish round" will consolidate and ship them as a batch.

## Test coverage

51 unit tests, all passing:
- 9 classifier tests (Round 7c)
- 4 backtest engine tests, basic + edge cases (Round 3)
- 3 backtest API wire format tests (Round 3)
- 6 HTTP client + EDGAR + yfinance fetch tests (Round 4)
- 2 predict ticker warning tests (Round 5)
- Plus 27 supporting tests across config hash, cache mechanisms, etc.

## Architecture patterns documented

`PATTERNS.md` codifies four engineering patterns the project has earned:
- **P-1** — CSS-vs-`[hidden]` companion rules (bit Round 2 modal, Round 6 banner)
- **P-2** — Avoid synchronous layout flushes after mutating large tables (bit Round 7a Leader Detector)
- **P-3** — Single-line PowerShell git commit messages (bit Round 7b regression-fix commit)
- **P-4** — Trace caller chains, not just function bodies (bit Round 7b shared-render-path bug)

Future rounds reference these patterns proactively in their prompts.

## How development happens here

Three-agent team via Claude Code Agent Teams:
- **Wright** — architecture review, design-first gating, structural concerns
- **Skipper** — implementation, tests, pipeline work
- **Sophia** — UX review, visual hierarchy, accessibility

Single-agent rounds for small focused work. Multi-agent rounds for architectural changes. Test-first discipline for new modules. Manual UI verification by the owner is mandatory for any round that touches frontend rendering.

Iteration cadence: each round on its own branch (`agent-roundXY`), verified locally, merged to main with explicit owner go-ahead.
