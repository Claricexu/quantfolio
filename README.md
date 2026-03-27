# Finance Ensemble Predictor

A machine learning-powered stock prediction dashboard that uses ensemble models (Random Forest, XGBoost, LightGBM) to predict next-day stock prices with a web-based UI.

## Architecture

```
Browser (UI)        FastAPI Server       ML Engine
index.html    <-->  api_server.py  -->  finance_model_v2.py (V2: RF+XGB)
(port 8000)                        -->  finance_model_v4_2pct.py (V4: Stacking Ensemble)
                                          |
                                   Yahoo Finance API + Local CSV Cache
```

## Features

- **Ticker Lookup** — Enter any ticker, run V2 or V4 model, or compare both side-by-side
- **Compare Both** — Run V2 and V4 simultaneously, see predictions and confidence
- **Daily Report** — Auto-scans 48+ symbols at market close, generates a unified report
- **Adjustable Parameters** — Tune training window and model threshold per prediction
- **SVR (Simple Value Ratio)** — Quick valuation check (Market Cap / Annualized Revenue)

## Models

| Model | Architecture | Strategy |
|---|---|---|
| **V2** (RF+XGB) | Random Forest + XGBoost ensemble | Direct signal prediction |
| **V4** (Stacking) | LightGBM + XGBoost + RF → Ridge meta-learner | +/-2% threshold: BUY/HOLD/SELL |

V4 achieved ~280% return with ~15% max drawdown in backtesting (2014-2026), compared to Buy & Hold's ~300% return but ~35% max drawdown.

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

## File Structure

```
Finance/
├── api_server.py            # FastAPI server (REST API + scheduling)
├── finance_model_v2.py      # V2 ML engine (RF + XGBoost)
├── finance_model_v4_2pct.py # V4 ML engine (Stacking Ensemble, best model)
├── frontend/
│   └── index.html           # Dashboard UI (single-page app)
├── Tickers.csv              # Symbol universe
├── requirements.txt         # Python dependencies
├── start_dashboard.bat      # One-click Windows launcher
└── data_cache/              # Auto-created: CSV cache + scan reports
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/predict/{symbol}` | Single ticker prediction (V2) |
| `GET /api/predict-compare/{symbol}` | Compare V2 vs V4 predictions |
| `GET /api/report` | Daily dual-model scan report |
| `GET /api/movers` | Cached daily scan results |
| `GET /api/symbols` | Full symbol universe |

## How It Works

1. Fetches historical price data from Yahoo Finance (cached locally)
2. Engineers 27-37 technical features (SMA, EMA, MACD, RSI, ATR, Bollinger Bands, etc.)
3. Trains ensemble models with walk-forward validation (no lookahead bias)
4. Predicts next-day close price and generates BUY/HOLD/SELL signals
5. V4 uses a +/-2% rescaled threshold — only acts on high-conviction predictions

## Adding Symbols

Edit `Tickers.csv` or modify `SYMBOL_UNIVERSE` in `finance_model_v2.py`, then restart the server.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| Yahoo Finance rate limit | Data is cached locally; delete `data_cache/*.csv` to refresh |
| Port 8000 in use | Edit `PORT = 8000` in `api_server.py` |
| No report data | Click Refresh — first scan takes 2-5 min |

## License

MIT
