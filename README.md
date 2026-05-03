# Quantfolio

A local-first ML toolkit for patient investors. Combines SEC fundamentals, peer-median benchmarking, and two walk-forward ensemble models — no cloud, no auth, runs on your laptop.

## Architecture

```
Browser (Dashboard)          FastAPI Server             Layer 2 — ML Engine
index.html             <-->  api_server.py       -->    Lite model (RF + XGBoost)
 ├─ Ticker Lookup                                 -->   Pro model (Stacking Ensemble)
 ├─ Daily Report                                         |
 ├─ Strategy Lab                                         backtest_engine.py
 └─ Leader Detector                                      (shared walk-forward simulator)
(port 8000)                                              |
                                                         Yahoo Finance + Local CSV Cache
                                                         |
                                                         Universe = leaders.csv (100)
                                                              ∪ Tickers.csv (85 manual)
                                                         ≈ 150 symbols (deduped)

                             Layer 1 — Leader Detector
                             universe_builder.py   -->   universe_raw.csv (2,501)
                                                   -->   universe_prescreened.csv (~1,400)
                             edgar_fetcher.py      -->   fundamentals.db (SEC XBRL, WAL)
                             classifier.py         -->   (sector, industry_group, industry)
                                                         via SIC ranges + 9 ticker overrides
                             fundamental_screener  -->   screener_results.csv (~1,400 rows,
                                                              verdict + archetype + score
                                                              + peer_median_* x 8 + peer_count)
                             verdict_provider.py   -->   unified verdict loader for all tabs
                             leader_selector.py    -->   leaders.csv (100 LEADER ∪ top GEM)
```

`backtest_engine.py` is the shared walk-forward simulator behind `predict_ticker`, `backtest_symbol`, and `backtest_multi_strategy` — one `BacktestConfig`, one config-hashed result, same numbers across the CLI, the dashboard, and the Daily Report.

## Features

- **Ticker Lookup** — Enter any ticker and get a Lite-vs-Pro prediction with consensus signal. Four-card valuation row (SVR / Market Cap / Quarterly Revenue / P/E) plus a verdict card with three-column metric grid showing the company value alongside its industry-group peer median.
- **Daily Report** — Auto-scans approximately 150 symbols at market close. Sortable table; click any row to expand the verdict card inline. Banner aggregates per-date close prices across symbols. A manual "Send Email Alert" button re-fires the email from the cached report without re-running the scan.
- **Strategy Lab** — Batch walk-forward backtesting across all tickers. Defaults to Daily Report symbols with an override toggle. Click any row to expand the equity-curve chart inline.
- **Leader Detector** — Browse the prescreened SEC universe (approximately 1,400 rows) with 4-verdict tags (LEADER / GEM / WATCH / AVOID), binary archetype (GROWTH vs MATURE), sector rank, and Good Firm score. Filter by verdict, archetype, sector, or industry group. Click any row to expand the verdict card inline.
- **Auto Strategy Mode** — ETFs use Full Signal (BUY+SELL), individual stocks use Buy-Only (BUY only, hold)
- **SVR (Simple Value Ratio)** — Quick valuation check (Market Cap / Annualized Revenue), displayed in predictions and reports. Email alerts include a Peer SVR column showing the industry-group median SVR alongside each ticker's SVR.
- **Best Strategy** — Each ticker's optimal risk-adjusted strategy (by Sharpe ratio) surfaced in lookup, report, and lab

## Dashboard Tabs

### Ticker Lookup
Enter a symbol and click **Predict**. Shows a Lite-vs-Pro side-by-side prediction with predicted price, percent change, signal (BUY/SELL/HOLD), consensus + confidence, per-model sub-predictions, and best backtest strategy. A four-card valuation row (SVR / Market Cap / Quarterly Revenue / P/E) sits above a three-column verdict card that pairs each metric with its industry-group peer median. The verdict card carries an "as of" timestamp chip showing screener freshness; hover the chip to see the raw ISO timestamp.

### Daily Report
Auto-generated at 4:05 PM EST on trading days. Three sortable tables (HIGH-CONFIDENCE BUY, HIGH-CONFIDENCE SELL, ALL SYMBOLS) with columns Symbol / Price / Lite Chg / Lite Sig / Pro Chg / Pro Sig / Consensus / Conf / Best Strategy / Firm Score. Banner above aggregates close-price dates across symbols. Click any row to expand the verdict card inline beneath that row.

### Strategy Lab
- **Run Batch Backtest** — Backtests all tickers with 5 strategies (Buy & Hold, Lite Buy-Only, Lite Full, Pro Buy-Only, Pro Full). Skips tickers with fresh cached results (< 7 days).
- **Default filter** — Defaults to the symbols in the most recent Daily Report. Toggle "Show all symbols" to bypass the filter; toggle does not persist across reloads.
- **Library Table** — Sortable results showing best strategy, Sharpe ratio, total return, max drawdown, and Sharpe vs B&H delta for every ticker.
- **Equity Curve Viewer** — Click any ticker row in the library to expand its interactive Chart.js equity curve inline beneath the clicked row, with all 5 strategy lines color-coded.

### Leader Detector
- **Universe Viewer** — Sortable table of all prescreened symbols (approximately 1,400). Columns: Symbol, Name, Sector, Market Cap, Verdict, Good Firm Score, Archetype, Sector Rank, Selected (✓ if in `leaders.csv`). Click any row to expand the verdict card inline beneath that row.
- **Filter Chips** — VERDICT (All / Leader / Gem / Watch / Avoid), ARCHETYPE (All / Growth / Mature), a SECTOR dropdown, and an INDUSTRY GROUP chip row that AND-combines with sector. All four filter families cross-narrow: picking Industry Group=Semiconductors trims the Sector dropdown; picking Sector=Technology trims the Industry Group chip pool.
- **Rebuild Now** — Kicks off the Layer 1 pipeline (`universe_builder.py` → `edgar_fetcher.py` → `fundamental_screener.py` → `leader_selector.py`). Warm rebuild ≈ 10 min; cold rebuild ≈ 3.5 hr (SEC EDGAR rate-limits at 10 req/sec).
- **Download CSV** — Export the currently filtered view for offline analysis.
- **Verdict Semantics** — `LEADER` = 5/5 archetype tests + top-5 market-cap rank in sector + no dealbreaker. `GEM` = 5/5 + outside top-5 rank. `WATCH` = 3–4/5. `AVOID` = ≤2/5 or any dealbreaker.

## Models

### Lite (RF + XGBoost)
- Random Forest (80% weight) + XGBoost (20% weight)
- 13 features: SMA, EMA, RSI, Bollinger Bands, returns, volatility
- Fixed blending weights, predictions clipped to +/-8%

### Pro (Stacking Ensemble)
- LightGBM + XGBoost + Random Forest
- 22 features: adds volume (OBV, Volume Z-score), momentum (ROC), trend strength (ADX, MACD), volatility (ATR, Garman-Klass), mean reversion (Z-score 50d), and lagged signals
- Inverse-MAE weighted average (weights optimized via 5-fold out-of-fold cross-validation)
- No prediction clipping — preserves full signal range
- **Requires `lightgbm`.** If `lightgbm` is not installed, Pro (v3) is unavailable and the relevant API fields (`v3`, `pro_*`) return `null`. The dashboard shows Lite-only output in that case. Install via `pip install -r requirements.txt` to enable Pro.

### Shared Design
- **Signal strategy**: Z-score +/-2.5 sigma relative to rolling 126-day prediction history
- **Retrain frequency**: Every 63 trading days (walk-forward, no lookahead bias)
- **Training window**: All data from 2010 to present (expanding window)
- **SVR filter**: BUY requires SVR <= 7, SELL if SVR >= 15
- **Auto strategy mode**: ETFs -> Full Signal (BUY+SELL), Stocks -> Buy-Only (BUY only)

### Strategy Modes

Backtest-validated across SPY, QQQ, MU, META, SMH:

| Mode | When | Why |
|---|---|---|
| **Full Signal** (BUY+SELL) | ETFs (SPY, QQQ, SMH, etc.) | SELL signals improve Sharpe by +0.06 to +0.20 on broad market instruments |
| **Buy-Only** (BUY only, hold) | Individual stocks (AAPL, MU, etc.) | SELL signals hurt Sharpe by -0.14 to -0.35 on volatile single names |
| **Auto** (default) | All tickers | Automatically detects ETFs vs stocks and applies the optimal strategy |

### Backtest Results

#### Old vs New Model Comparison (SPY, 2015-2026)

| Metric | Old Lite | New Lite | Old Pro | New Pro | Buy & Hold |
|---|---|---|---|---|---|
| Total Return | +36.2% | +135.6% | +282.3% | +431.7% | +285.2% |
| Annual Return | +2.8% | +7.9% | +12.7% | +16.1% | +12.8% |
| Sharpe Ratio | 0.26 | 0.59 | 0.76 | 0.98 | 0.77 |
| Sharpe 95% CI | [-0.33,+0.85] | [-0.01,+1.18] | [+0.17,+1.36] | [+0.38,+1.58] | [+0.17,+1.36] |
| Max Drawdown | -42.3% | -30.0% | -33.7% | -33.7% | -33.7% |
| Trades | 22 | 32 | 1 | 18 | 0 |

Key improvements: Lite Sharpe 0.26 -> 0.59 (+0.33), Pro Sharpe 0.76 -> 0.98 (+0.22).

#### Buy-Only vs Full Signal (Cross-Symbol Sharpe)

| Symbol | Pro Full | Pro Buy-Only | Lite Full | Lite Buy-Only | B&H |
|---|---|---|---|---|---|
| SPY | 0.98 | 0.82 | 0.59 | 0.77 | 0.76 |
| QQQ | 0.90 | 0.84 | 0.68 | 0.85 | 0.85 |
| MU | 0.38 | 0.73 | 0.42 | 0.77 | 0.66 |
| META | 0.52 | 0.67 | 0.52 | 0.66 | 0.66 |
| SMH | 1.15 | 0.95 | 0.97 | 0.94 | 0.94 |
| **Average** | **0.79** | **0.80** | **0.64** | **0.80** | **0.77** |

## Good Firm Framework

Layer 1 applies a fundamentals filter to the entire SEC-registered US equity universe (≥ $1B market cap, price > $3, avg dollar volume > $1M) before Layer 2's ML models ever see a ticker. The filter is archetype-dispatched:

### Archetype Classifier (binary, T = 12%)

```
archetype = GROWTH  if Revenue YoY ≥ 12%
          = MATURE  otherwise
```

Threshold locked via `diag_threshold_sensitivity.py` — stable across 10/12/15/20% bands.

### Test Suites (5 per archetype)

| Archetype | Tests | Dealbreakers |
|---|---|---|
| **MATURE** | not_declining (Revenue 3Y CAGR ≥ 0%), margin_quality (Operating Margin ≥ 5%), cash_generation (FCF/Revenue ≥ 5%), moat (ROIC ≥ 10%), stability (Revenue CAGR stdev < 15%) | cagr_shrinking (3Y CAGR < −5%), diluting (shares_out 3Y CAGR > 5%) |
| **GROWTH** | growth_rate (Revenue YoY ≥ 12%), unit_economics (Gross Margin ≥ 40% or improving), path_to_profits (Operating Margin trend positive or ≥ 0%), moat (Gross Margin ≥ 30% or R&D/Rev ≥ 10%), capital_efficiency (Rule of 40 ≥ 30%) | burning_cash (FCF/Revenue < −15%) |

### Verdict Mapping (4-tier + insufficient-data sentinel)

| Verdict | Rule |
|---|---|
| **LEADER** | 5/5 tests pass AND market-cap rank in sector ≤ 5 AND no dealbreaker |
| **GEM** | 5/5 tests pass AND sector rank > 5 AND no dealbreaker |
| **WATCH** | 3–4/5 tests pass AND no dealbreaker |
| **AVOID** | ≤ 2/5 tests pass OR any dealbreaker flag set |
| **INSUFFICIENT_DATA** | Missing key metrics (< 3 years of filings, etc.) |

Full rubric + rationale + Phase 2 backlog: see [`good_firm_framework.md`](good_firm_framework.md).

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure `.env` (optional but recommended)

Copy `.env.example` → `.env` and fill in the values you need. `.env` is gitignored; it never leaves your machine.

```bash
cp .env.example .env
# then edit .env in your editor of choice
```

| Variable | What it does |
|---|---|
| `SMTP_ENABLED`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_TO` | Email alerts after each 4:05 PM scan. For Gmail, use an App Password from https://myaccount.google.com/apppasswords. Leave `SMTP_ENABLED=false` to skip. |
| `SEC_USER_AGENT`, `SEC_CONTACT_EMAIL` | SEC EDGAR requires a real contact email in the User-Agent header for the Leader Detector pipeline. Without this, SEC will rate-limit or block you. |

End users: see [USER_GUIDE.md](USER_GUIDE.md) Part 1, Step 4 for a non-technical walkthrough.

### 3. Launch

**Windows** — Double-click `start_dashboard.bat`

**Terminal:**
```bash
python api_server.py
```

### 4. Open browser

Navigate to **http://localhost:8000**

The dashboard opens with four tabs: Ticker Lookup, Daily Report, Strategy Lab, and Leader Detector.

## CLI Usage

```bash
# Single ticker prediction (auto strategy)
python finance_model_v2.py --ticker AAPL

# Force strategy mode
python finance_model_v2.py --ticker SPY --strategy full
python finance_model_v2.py --ticker MU --strategy buy_only

# Backtest
python finance_model_v2.py --backtest SPY
python finance_model_v2.py --backtest MU --strategy buy_only --version v3

# Daily scan report
python finance_model_v2.py --report

# Old vs New model comparison (any cached ticker)
python backtest_old_vs_new.py SPY
python backtest_old_vs_new.py QQQ

# Buy-Only vs Full Signal backtest (multiple tickers)
python backtest_buy_hold.py SPY QQQ AAPL NVDA MSFT
```

### Layer 1 — Leader Detector pipeline

```bash
# Full pipeline (universe build + prescreen) — ~135 min cold, ~1 min warm
python universe_builder.py --build

# Prescreen only (no SEC calls, local universe_raw.csv -> universe_prescreened.csv)
python universe_builder.py --prescreen-only

# Fetch SEC XBRL facts for prescreened universe — ~200 min cold, seconds warm
python edgar_fetcher.py --universe universe_prescreened.csv

# Compute metrics + assign verdicts (all rows in fundamentals.db) — ~5 min
python fundamental_screener.py --all

# Pick top 100 leaders (LEADER ∪ top-GEM) -> leaders.csv — seconds
python leader_selector.py --build
```

## File Structure

```
Finance/
├── api_server.py                # FastAPI server (REST API + scheduling)
│
├── Layer 2 — ML Engine ───────────────────────────────────────────────
├── finance_model_v2.py          # Core ML engine (Lite + Pro models)
├── backtest_engine.py           # Shared walk-forward simulator (BacktestEngine)
├── finance_model_v4_2pct.py     # Lite vs Pro backtest with sensitivity analysis
├── backtest_old_vs_new.py       # Old (GitHub) vs New model comparison
├── backtest_buy_hold.py         # Buy-Only vs Full Signal backtest (multi-symbol)
├── backtest_diverse_ensemble.py # LGB+RF+MLP diverse ensemble test
│
├── Layer 1 — Leader Detector ─────────────────────────────────────────
├── universe_builder.py          # Phase 1.0 + 1.1: pulls SEC tickers, applies prescreen
├── edgar_fetcher.py             # Phase 1.2: pulls XBRL facts into fundamentals.db (WAL)
├── fundamental_metrics.py       # Phase 1.3a: metric computations + archetype classifier
├── classifier.py                # Round 7c: SIC + ticker overrides → (sector, industry_group, industry)
├── fundamental_screener.py      # Phase 1.3b: archetype-routed tests, verdict, peer medians
├── verdict_provider.py          # Unified verdict loader (single source of truth across tabs)
├── leader_selector.py           # Phase 1.4: LEADER ∪ top-GEM -> leaders.csv
├── prescreen_rules.json         # 6-rule prescreen config (liquidity, filings, SIC, SVR)
├── good_firm_framework.md       # Framework spec (archetypes, tests, verdicts)
│
├── tests/
│   ├── unit/                    # 108 plain-assert unit tests (BacktestEngine, classifier, peer median, HTTP, signal alerts, manual-send, biweekly refresh, Case B retry)
│   └── backtest_baselines/      # Live-vs-live verify scripts (C-3 regression barrier)
│
├── frontend/
│   └── index.html               # Dashboard UI (single-page app, 4 tabs)
├── Tickers.csv                  # Manual watchlist (85 symbols, unions with leaders.csv)
├── leaders.csv                  # Layer 1 output (100 automated picks)
├── screener_results.csv         # Full screener output (~1,400 rows; feeds Leader Detector tab)
├── universe_raw.csv             # Phase 1.0 output (2,501 SEC-registered tickers)
├── universe_prescreened.csv     # Phase 1.1 output (~1,400 after prescreen)
├── fundamentals.db              # SQLite XBRL cache (SEC EDGAR facts, WAL mode)
├── requirements.txt             # Python dependencies (pinned)
├── requirements.lock            # pip freeze capture for reproducibility
├── .env.example                 # Template for SMTP + SEC credentials
├── start_dashboard.bat          # One-click Windows launcher (full dep-check on startup)
├── CHANGELOG.md                 # User-facing release notes
└── data_cache/                  # Auto-created: CSV cache, scan reports, backtest JSONs
```

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/predict/{symbol}` | Single ticker prediction (`?version=v3&strategy=auto`) |
| GET | `/api/predict-compare/{symbol}` | Compare Lite vs Pro with consensus signal and best strategy |
| GET | `/api/report` | Daily dual-model scan report (`?refresh=true` to trigger fresh scan) |
| GET | `/api/movers` | Cached daily scan results (`?refresh=true&version=v3`) |
| GET | `/api/symbols` | Full symbol universe with ETF list and strategy info |
| GET | `/api/backtest-chart/{symbol}` | Walk-forward backtest with 5 strategy equity curves (`?refresh=true`) |
| GET | `/api/backtest-library` | Summary stats for all cached backtests |
| POST | `/api/backtest-batch` | Start batch backtest for all uncached symbols |
| GET | `/api/backtest-batch/status` | Poll batch backtest progress |
| GET | `/api/leaders` | Current `leaders.csv` (100 rows) with verdict, archetype, score |
| GET | `/api/universe` | Screener rows. `?source=screener` (default, ~1,400 rows w/ verdicts) \| `prescreened` \| `raw` |
| GET | `/api/screener/{symbol}` | Single-ticker screener row (verdict card in Ticker Lookup) |
| POST | `/api/leaders/rebuild` | Kick off the quarterly Layer 1 rebuild pipeline |
| GET | `/api/leaders/rebuild/status` | Poll rebuild progress (stage, % complete, last error) |
| POST | `/api/alerts/send-manual` | Re-send the signal-brief email from the cached daily report |
| GET | `/api/alerts/config` | SMTP enabled flag + recipient count for the manual-trigger confirmation dialog |
| GET | `/api/backtest-refresh/status` | Biweekly backtest refresh status: last run, next run, Case B history |

Strategy parameter: `?strategy=auto` (default), `?strategy=full`, `?strategy=buy_only`

**Model availability:** prediction responses include `v2` (Lite) and `v3` (Pro) fields. `v3` may be `null` when `lightgbm` is not installed — callers should treat `null` as "Pro unavailable, fall back to Lite" rather than an error.

**Prediction warnings:** prediction responses include a `warnings` array; `stale_features_used` indicates today's feature row had NaN values and yesterday's features were used as fallback. Compare-card responses also mirror these as `v2_warnings` / `v3_warnings` at the top level.

## Email Alerts

After each scheduled scan (and on demand from the Daily Report's manual button), Quantfolio classifies every report row through `_classify_alert` ([api_server.py:125-179](api_server.py#L125-L179) — the canonical rule docstring) and emails a Signal Brief if anything qualifies:

- **Consensus BUY** — both Lite and Pro signal BUY.
- **Single-model BUY (Pro-only)** — Pro signals BUY, Lite=HOLD, validated when `best_strategy ∈ {pro_buyonly, pro_full}`.
- **Single-model BUY (Lite-only)** — Lite signals BUY, Pro=HOLD, validated when `best_strategy ∈ {lite_buyonly, lite_full}`.
- **SELL gate** — suppress if `best_strategy ∈ {buyhold, lite_buyonly, pro_buyonly}` (acting on a SELL would have hurt these tickers historically).
- **SELL fires** only on `pro_full` + Pro=SELL or `lite_full` + Lite=SELL.
- **Conflict** (Lite/Pro disagree BUY vs SELL) is never alerted.

Each row in the email carries the ticker's SVR plus the industry-group peer median SVR for context (`_peer_svr_str`, [api_server.py:226-251](api_server.py#L226-L251)). The Daily Report tab also exposes a **Send Email Alert** button that re-fires the brief from the most recent cached report — useful if SMTP was misconfigured during the scheduled run.

## Scheduling

When APScheduler is installed, the server automatically runs the dual-model report at **4:05 PM EST, Monday-Friday** (just after market close). Results are cached and served via `/api/report`.

The Layer 1 pipeline rebuilds on a quarterly cadence: **Feb 15 / May 15 / Aug 15 / Nov 15 at 2 AM** (timed ~45 days after each 10-Q filing window closes, so fresh fundamentals are reflected in the next rebuild). Trigger a manual rebuild any time via the Leader Detector tab's **Rebuild Now** button or `POST /api/leaders/rebuild`.

Every other Friday at 9 PM ET, a biweekly backtest refresh sweeps tickers whose cached backtest is older than 15 days. Tickers that fail with insufficient-data ("Case B") are deferred for 8 weeks before retry. Status: `GET /api/backtest-refresh/status`.

Without APScheduler, trigger manually: `GET /api/report?refresh=true`

## How It Works

1. Fetches historical price data from Yahoo Finance (cached locally, from 2010)
2. Engineers 13-22 technical features (SMA, MACD, RSI, ATR, Bollinger Bands, OBV, ADX, etc.)
3. Trains ensemble models with walk-forward validation (expanding window, no lookahead bias)
4. Predicts next-day close price, converts to Z-score relative to rolling 126-day prediction history
5. Generates signals based on strategy mode:
   - **Full Signal** (ETFs): BUY at Z >= 2.5 sigma, SELL at Z <= -2.5 sigma
   - **Buy-Only** (Stocks): BUY at Z >= 2.5 sigma, never sell (hold)
6. SVR valuation filter overrides: BUY blocked if SVR > 7, SELL forced if SVR >= 15
7. Walk-forward backtest compares 5 strategies over 10+ years, selects best by Sharpe ratio
8. Quarterly, the Layer 1 pipeline (`universe_builder.py` → `edgar_fetcher.py` → `fundamental_screener.py` → `leader_selector.py`) regenerates `leaders.csv` from SEC XBRL filings. The trading universe (approximately 150 symbols) = `leaders.csv` ∪ `Tickers.csv` (deduped)

## Adding Symbols

Edit `Tickers.csv` or modify `SYMBOL_UNIVERSE` in `finance_model_v2.py`, then restart the server. ETFs added to `ETF_TICKERS` in `finance_model_v2.py` will automatically use Full Signal strategy.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| Yahoo Finance rate limit | Data is cached locally; delete `data_cache/*.csv` to refresh |
| Port 8000 in use | Edit `PORT = 8000` in `api_server.py` |
| No report data | Click Refresh — a full scan across approximately 150 symbols typically takes 25-55 min on a laptop (cold; subsequent scans reuse price-cache) |
| RF predict hangs on Windows | All models use `n_jobs=1` to avoid joblib deadlock |
| Batch backtest stuck | Check server console for errors; restart server if needed |
| Best Strategy shows "—" | Run batch backtest in Strategy Lab to generate data |
| Leader Detector table empty | Click Rebuild Now. First build takes ~3.5 hr cold (SEC rate limits); warm rebuilds ~10 min |
| Rebuild stuck at "Fetching XBRL" | SEC EDGAR enforces 10 req/sec — wait, don't retry. Resume-safe (cached rows skipped) |
| `leaders.csv` missing | Run `python leader_selector.py --build` (requires `screener_results.csv` + `universe_raw.csv`) |

## License

MIT
