# Quantfolio

A patient investor's ML toolkit that combines quantitative signals with value principles. One-click dashboard, two ensemble models, walk-forward backtesting, zero dependencies on Wall Street.

## Architecture

```
Browser (Dashboard)          FastAPI Server             Layer 2 — ML Engine
index.html             <-->  api_server.py       -->    Lite model (RF + XGBoost)
 ├─ Ticker Lookup                                 -->   Pro model (Stacking Ensemble)
 ├─ Daily Report                                         |
 ├─ Strategy Lab                                         Yahoo Finance + Local CSV Cache
 └─ Leader Detector                                      |
(port 8000)                                              Universe = leaders.csv (100)
                                                              ∪ Tickers.csv (85 manual)
                                                         = 174 symbols (deduped)

                             Layer 1 — Leader Detector
                             universe_builder.py   -->   universe_raw.csv (2,501)
                                                   -->   universe_prescreened.csv (1,414)
                             edgar_fetcher.py      -->   fundamentals.db (SEC XBRL)
                             fundamental_screener  -->   screener_results.csv (1,414 rows,
                                                              verdict + archetype + score)
                             leader_selector.py    -->   leaders.csv (100 LEADER ∪ top GEM)
```

## Features

- **Ticker Lookup** — Enter any ticker, run Lite or Pro model, or compare both side-by-side with consensus signal
- **Compare Both** — Run Lite and Pro simultaneously with confidence scoring and best strategy recommendation
- **Daily Report** — Auto-scans 174 symbols (100 automated leaders ∪ 85 manual watchlist, deduped) at market close, generates a sortable report with best strategy per ticker
- **Strategy Lab** — Batch walk-forward backtesting across all tickers, equity curve charting, and strategy comparison library
- **Leader Detector** — Browse the 1,414-row prescreened SEC universe with 4-verdict tags (LEADER / GEM / WATCH / AVOID), binary archetype (GROWTH vs MATURE), sector rank, and Good Firm score. Filter by verdict, archetype, or sector; trigger quarterly rebuild; download CSV.
- **Auto Strategy Mode** — ETFs use Full Signal (BUY+SELL), individual stocks use Buy-Only (BUY only, hold)
- **SVR (Simple Value Ratio)** — Quick valuation check (Market Cap / Annualized Revenue), displayed in predictions and reports
- **Best Strategy** — Each ticker's optimal risk-adjusted strategy (by Sharpe ratio) surfaced in lookup, report, and lab

## Dashboard Tabs

### Ticker Lookup
Enter a symbol and click **Predict** (single model) or **Compare Both** (Lite vs Pro). Shows predicted price, percent change, signal (BUY/SELL/HOLD), SVR valuation, model sub-predictions, and best backtest strategy with Sharpe ratio.

### Daily Report
Auto-generated at 4:05 PM EST on trading days. Sortable table with columns: Symbol, Price, Change, Lite Signal, Pro Signal, Consensus, Confidence, Best Strategy. Click any column header to sort. Color-coded signals and confidence levels.

### Strategy Lab
- **Run Batch Backtest** — Backtests all tickers with 5 strategies (Buy & Hold, Lite Buy-Only, Lite Full, Pro Buy-Only, Pro Full). Skips tickers with fresh cached results (< 7 days).
- **Library Table** — Sortable results showing best strategy, Sharpe ratio, total return, max drawdown, and Sharpe vs B&H delta for every ticker.
- **Equity Curve Viewer** — Click any ticker row to load its interactive Chart.js equity curve with all 5 strategy lines color-coded.

### Leader Detector
- **Universe Viewer** — Sortable table of all 1,414 prescreened symbols. Columns: Symbol, Name, Sector, Market Cap, Verdict, Good Firm Score, Archetype, Sector Rank, Selected (✓ if in `leaders.csv`).
- **Filter Chips** — VERDICT (All / Leader / Gem / Watch / Avoid), ARCHETYPE (All / Growth / Mature), and a SECTOR dropdown populated from the live dataset.
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

### 2. Launch

**Windows** — Double-click `start_dashboard.bat`

**Terminal:**
```bash
python api_server.py
```

### 3. Open browser

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
├── finance_model_v4_2pct.py     # Lite vs Pro backtest with sensitivity analysis
├── backtest_old_vs_new.py       # Old (GitHub) vs New model comparison
├── backtest_buy_hold.py         # Buy-Only vs Full Signal backtest (multi-symbol)
├── backtest_diverse_ensemble.py # LGB+RF+MLP diverse ensemble test
│
├── Layer 1 — Leader Detector ─────────────────────────────────────────
├── universe_builder.py          # Phase 1.0 + 1.1: pulls SEC tickers, applies prescreen
├── edgar_fetcher.py             # Phase 1.2: pulls XBRL facts into fundamentals.db
├── fundamental_metrics.py       # Phase 1.3a: metric computations + archetype classifier
├── fundamental_screener.py      # Phase 1.3b: archetype-routed tests, verdict assignment
├── leader_selector.py           # Phase 1.4: LEADER ∪ top-GEM -> leaders.csv
├── prescreen_rules.json         # Prescreen thresholds (market cap, price, volume)
├── good_firm_framework.md       # Framework spec (archetypes, tests, verdicts)
│
├── frontend/
│   └── index.html               # Dashboard UI (single-page app, 4 tabs)
├── Tickers.csv                  # Manual watchlist (85 symbols, unions with leaders.csv)
├── leaders.csv                  # Layer 1 output (100 automated picks)
├── screener_results.csv         # Full 1,414-row screener output (feeds Leader Detector tab)
├── universe_raw.csv             # Phase 1.0 output (2,501 SEC-registered tickers)
├── universe_prescreened.csv     # Phase 1.1 output (1,414 after prescreen)
├── fundamentals.db              # SQLite XBRL cache (SEC EDGAR facts)
├── requirements.txt             # Python dependencies
├── start_dashboard.bat          # One-click Windows launcher
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
| GET | `/api/universe` | Screener rows. `?source=screener` (default, 1,414 rows w/ verdicts) \| `prescreened` \| `raw` |
| GET | `/api/screener/{symbol}` | Single-ticker screener row (verdict card in Ticker Lookup) |
| POST | `/api/leaders/rebuild` | Kick off the quarterly Layer 1 rebuild pipeline |
| GET | `/api/leaders/rebuild/status` | Poll rebuild progress (stage, % complete, last error) |

Strategy parameter: `?strategy=auto` (default), `?strategy=full`, `?strategy=buy_only`

## Scheduling

When APScheduler is installed, the server automatically runs the dual-model report at **4:05 PM EST, Monday-Friday** (just after market close). Results are cached and served via `/api/report`.

The Layer 1 pipeline rebuilds on a quarterly cadence: **Feb 15 / May 15 / Aug 15 / Nov 15 at 2 AM** (timed ~45 days after each 10-Q filing window closes, so fresh fundamentals are reflected in the next rebuild). Trigger a manual rebuild any time via the Leader Detector tab's **Rebuild Now** button or `POST /api/leaders/rebuild`.

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
8. Quarterly, the Layer 1 pipeline (`universe_builder.py` → `edgar_fetcher.py` → `fundamental_screener.py` → `leader_selector.py`) regenerates `leaders.csv` from SEC XBRL filings. The 174-symbol trading universe = `leaders.csv` ∪ `Tickers.csv` (deduped)

## Adding Symbols

Edit `Tickers.csv` or modify `SYMBOL_UNIVERSE` in `finance_model_v2.py`, then restart the server. ETFs added to `ETF_TICKERS` in `finance_model_v2.py` will automatically use Full Signal strategy.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| Yahoo Finance rate limit | Data is cached locally; delete `data_cache/*.csv` to refresh |
| Port 8000 in use | Edit `PORT = 8000` in `api_server.py` |
| No report data | Click Refresh — first scan takes 2-5 min |
| RF predict hangs on Windows | All models use `n_jobs=1` to avoid joblib deadlock |
| Batch backtest stuck | Check server console for errors; restart server if needed |
| Best Strategy shows "—" | Run batch backtest in Strategy Lab to generate data |
| Leader Detector table empty | Click Rebuild Now. First build takes ~3.5 hr cold (SEC rate limits); warm rebuilds ~10 min |
| Rebuild stuck at "Fetching XBRL" | SEC EDGAR enforces 10 req/sec — wait, don't retry. Resume-safe (cached rows skipped) |
| `leaders.csv` missing | Run `python leader_selector.py --build` (requires `screener_results.csv` + `universe_raw.csv`) |

## License

MIT
