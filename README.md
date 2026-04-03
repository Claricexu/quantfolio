# Quantfolio

A patient investor's ML toolkit that combines quantitative signals with value principles. One-click dashboard, two ensemble models, zero dependencies on Wall Street.

## Architecture

```
Browser (UI)        FastAPI Server       ML Engine
index.html    <-->  api_server.py  -->  Lite model (RF + XGBoost)
(port 8000)                        -->  Pro model (Stacking Ensemble)
                                          |
                                   Yahoo Finance API + Local CSV Cache
```

## Features

- **Ticker Lookup** — Enter any ticker, run Lite or Pro model, or compare both side-by-side
- **Compare Both** — Run Lite and Pro simultaneously, see predictions and confidence
- **Daily Report** — Auto-scans 73+ symbols at market close, generates a unified report with sortable columns
- **Auto Strategy Mode** — ETFs use Full Signal (BUY+SELL), individual stocks use Buy-Only (BUY only, hold)
- **SVR (Simple Value Ratio)** — Quick valuation check (Market Cap / Annualized Revenue)

## Models

### Lite (RF + XGBoost)
- Random Forest (80% weight) + XGBoost (20% weight)
- 13 features: SMA, EMA, RSI, Bollinger Bands, returns, volatility
- Fixed blending weights, predictions clipped to ±8%

### Pro (Stacking Ensemble)
- LightGBM + XGBoost + Random Forest
- 22 features: adds volume (OBV, Volume Z-score), momentum (ROC), trend strength (ADX, MACD), volatility (ATR, Garman-Klass), mean reversion (Z-score 50d), and lagged signals
- Inverse-MAE weighted average (weights optimized via 5-fold out-of-fold cross-validation)
- No prediction clipping — preserves full signal range

### Shared Design
- **Signal strategy**: Z-score ±2.5σ relative to rolling 126-day prediction history
- **Retrain frequency**: Every 63 trading days (walk-forward, no lookahead bias)
- **Training window**: All data from 2010 to present (expanding window)
- **SVR filter**: BUY requires SVR ≤ 7, SELL if SVR ≥ 15
- **Auto strategy mode**: ETFs → Full Signal (BUY+SELL), Stocks → Buy-Only (BUY only)

### Strategy Modes

Backtest-validated across SPY, QQQ, MU, META, SMH:

| Mode | When | Why |
|---|---|---|
| **Full Signal** (BUY+SELL) | ETFs (SPY, QQQ, SMH, etc.) | SELL signals improve Sharpe by +0.06 to +0.20 on broad market instruments |
| **Buy-Only** (BUY only, hold) | Individual stocks (AAPL, MU, etc.) | SELL signals hurt Sharpe by -0.14 to -0.35 on volatile single names |
| **Auto** (default) | All tickers | Automatically detects ETFs vs stocks and applies the optimal strategy |

### Backtest Results

#### Old vs New Model Comparison (SPY, 2015–2026)

| Metric | Old Lite | New Lite | Old Pro | New Pro | Buy & Hold |
|---|---|---|---|---|---|
| Total Return | +36.2% | +135.6% | +282.3% | +431.7% | +285.2% |
| Annual Return | +2.8% | +7.9% | +12.7% | +16.1% | +12.8% |
| Sharpe Ratio | 0.26 | 0.59 | 0.76 | 0.98 | 0.77 |
| Sharpe 95% CI | [-0.33,+0.85] | [-0.01,+1.18] | [+0.17,+1.36] | [+0.38,+1.58] | [+0.17,+1.36] |
| Max Drawdown | -42.3% | -30.0% | -33.7% | -33.7% | -33.7% |
| Trades | 22 | 32 | 1 | 18 | 0 |

Key improvements: Lite Sharpe 0.26 → 0.59 (+0.33), Pro Sharpe 0.76 → 0.98 (+0.22).

#### Buy-Only vs Full Signal (Cross-Symbol Sharpe)

| Symbol | Pro Full | Pro Buy-Only | Lite Full | Lite Buy-Only | B&H |
|---|---|---|---|---|---|
| SPY | 0.98 | 0.82 | 0.59 | 0.77 | 0.76 |
| QQQ | 0.90 | 0.84 | 0.68 | 0.85 | 0.85 |
| MU | 0.38 | 0.73 | 0.42 | 0.77 | 0.66 |
| META | 0.52 | 0.67 | 0.52 | 0.66 | 0.66 |
| SMH | 1.15 | 0.95 | 0.97 | 0.94 | 0.94 |
| **Average** | **0.79** | **0.80** | **0.64** | **0.80** | **0.77** |

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

## File Structure

```
Finance/
├── api_server.py                # FastAPI server (REST API + scheduling)
├── finance_model_v2.py          # Core ML engine (Lite + Pro models)
├── finance_model_v4_2pct.py     # Lite vs Pro backtest with sensitivity analysis
├── backtest_old_vs_new.py       # Old (GitHub) vs New model comparison
├── backtest_buy_hold.py         # Buy-Only vs Full Signal backtest (multi-symbol)
├── backtest_momentum_chaser.py  # Momentum chaser backtest (ON/O2O strategies)
├── backtest_diverse_ensemble.py # LGB+RF+MLP diverse ensemble test
├── frontend/
│   └── index.html               # Dashboard UI (single-page app)
├── Tickers.csv                  # Symbol universe (73 tickers)
├── requirements.txt             # Python dependencies
├── start_dashboard.bat          # One-click Windows launcher
└── data_cache/                  # Auto-created: CSV cache + scan reports
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/predict/{symbol}` | Single ticker prediction (`?version=v3&strategy=auto`) |
| `GET /api/predict-compare/{symbol}` | Compare Lite vs Pro (`?strategy=auto`) |
| `GET /api/report` | Daily dual-model scan report |
| `GET /api/movers` | Cached daily scan results |
| `GET /api/symbols` | Full symbol universe + ETF list + strategy info |

Strategy parameter: `?strategy=auto` (default), `?strategy=full`, `?strategy=buy_only`

## How It Works

1. Fetches historical price data from Yahoo Finance (cached locally, from 2010)
2. Engineers 13–22 technical features (SMA, MACD, RSI, ATR, Bollinger Bands, OBV, ADX, etc.)
3. Trains ensemble models with walk-forward validation (expanding window, no lookahead bias)
4. Predicts next-day close price, converts to Z-score relative to rolling 126-day prediction history
5. Generates signals based on strategy mode:
   - **Full Signal** (ETFs): BUY at Z ≥ 2.5σ, SELL at Z ≤ -2.5σ
   - **Buy-Only** (Stocks): BUY at Z ≥ 2.5σ, never sell (hold)
6. SVR valuation filter overrides: BUY blocked if SVR > 7, SELL forced if SVR ≥ 15

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

## License

MIT
