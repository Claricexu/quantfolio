"""
Quantfolio — ML Prediction Engine (Lite + Pro)
===============================================
Primary: V3 Pro Stacking (LightGBM + XGBoost + RF -> inverse-MAE weighted), 22 features
Fallback: V2 Lite (RF 80% + XGB 20%), 13 features — if LightGBM unavailable

Key innovations (backtest-proven):
  - Inverse-MAE weighted ensemble (replaces Ridge meta that compressed signals)
  - Z-score ±2.5 sigma signal strategy (replaces absolute ±2% that never triggered)
  - SVR valuation filter: BUY requires SVR<=7, SELL if SVR>=15
  - Walk-forward with 63-day retrain aligned to 126-day Z-score lookback
  - Auto strategy mode: ETFs → Full Signal (BUY+SELL), Stocks → Buy-Only (BUY only)
    Backtest-validated: SELL signals improve ETF Sharpe (+0.06 to +0.20) but hurt
    individual stocks (-0.14 to -0.35) due to false exits in volatile names

Usage:
  python finance_model_v2.py --ticker AAPL
  python finance_model_v2.py --ticker SPY --strategy full
  python finance_model_v2.py --backtest SPY
  python finance_model_v2.py --backtest MU --strategy buy_only
  python finance_model_v2.py --report
"""

import pandas as pd
import numpy as np
import warnings
import json
import argparse
import os
import time
import random
from datetime import datetime, timedelta

from ta.trend import SMAIndicator, EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator, ROCIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import OnBalanceVolumeIndicator

from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_absolute_error, mean_squared_error
import xgboost as xgb

try:
    import lightgbm as lgb
    HAS_LGBM = True
except ImportError:
    HAS_LGBM = False
    print("[info] lightgbm not installed — using V2 (RF+XGB) only.")

import yfinance as yf

warnings.filterwarnings('ignore')
os.environ['PYTHONWARNINGS'] = 'ignore'

# =============================================================================
# CONFIGURATION
# =============================================================================
DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_cache')
CACHE_DIR = os.environ.get("FINANCE_CACHE_DIR", DEFAULT_CACHE_DIR)
CACHE_DAYS = 1
FETCH_DELAY_SEC = 1.5
MAX_RETRIES = 5
BACKOFF_BASE = 2
THRESHOLD = 2.5          # Z-score threshold (±2.5 sigma — optimal per sensitivity analysis)
ZSCORE_LOOKBACK = 126    # ~6 months rolling window for Z-score computation
RETRAIN_FREQ_V2 = 63     # aligned with Z-score lookback (was 20, caused stale cash positions)
RETRAIN_FREQ_V3 = 63
BACKTEST_PREFERRED_START = '2015-01-02'   # preferred start — gives 5 yr training from 2010
MIN_TRAIN_DAYS = 126     # ~6 months minimum training data (was 252; lowered for newer IPOs like TEM)
MIN_BACKTEST_DAYS = 50   # need at least 50 days for meaningful backtest
MIN_ZSCORE_SAMPLES = 20  # minimum predictions before Z-score signals activate
MODEL_VERSION = 'v3' if HAS_LGBM else 'v2'

# Strategy mode: "full" (BUY+SELL), "buy_only" (BUY only, hold forever), "auto" (ETF→full, stock→buy_only)
# Backtest-validated: SELL signals help on ETFs (Sharpe +0.06 to +0.20) but hurt on
# individual stocks (Sharpe -0.14 to -0.35). Auto mode applies the right strategy per ticker.
DEFAULT_STRATEGY = 'auto'

# Known ETF tickers — used by auto mode to choose full signal strategy
ETF_TICKERS = {
    # Major index ETFs
    'SPY', 'QQQ', 'IWM', 'DIA', 'VOO', 'VTI', 'RSP',
    # Sector ETFs
    'SMH', 'XLE', 'XOP', 'XLF', 'XLK', 'XLV', 'XLI', 'XLU', 'XLP', 'XLY', 'XLC', 'XLB',
    'SOXX', 'IBB', 'KRE', 'KWEB',
    # Bond ETFs
    'TLT', 'TIP', 'BND', 'AGG', 'HYG', 'LQD', 'SHY', 'IEF',
    # Commodity ETFs
    'GLD', 'SLV', 'USO', 'UNG', 'DBA',
    # Thematic / International ETFs
    'FXI', 'EEM', 'EFA', 'VWO', 'IBIT', 'URNM', 'ARKK', 'ARKG',
}

def get_strategy_mode(symbol, override=None):
    """Determine strategy mode for a symbol.
    Returns 'full' or 'buy_only'."""
    if override and override in ('full', 'buy_only'):
        return override
    if override == 'auto' or override is None:
        return 'full' if symbol.upper() in ETF_TICKERS else 'buy_only'
    return 'buy_only'  # safe default

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TICKERS_CSV = os.path.join(SCRIPT_DIR, "Tickers.csv")
LEADERS_CSV = os.path.join(SCRIPT_DIR, "leaders.csv")  # Phase 1.5 — Layer 1 handoff
_DEFAULT_SYMBOLS = ["SPY","QQQ","AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AMD","AVGO","JPM","UNH","XOM","TLT","GLD"]


def _read_leaders_csv(path):
    """Read symbol column from leaders.csv (Phase 1.4 output).
    Returns [] on any failure so get_all_symbols() can fall through to Tickers.csv."""
    import csv as _csv  # local import — top of file doesn't import csv
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            reader = _csv.DictReader(f)
            if not reader.fieldnames or 'symbol' not in reader.fieldnames:
                return []
            out = []; seen = set()
            for row in reader:
                sym = (row.get('symbol') or '').strip().upper()
                if sym and sym not in seen:
                    seen.add(sym); out.append(sym)
            return out
    except Exception:
        return []


def _read_tickers_csv(path):
    """Read plain-text ticker list (one per line, `#` = comment) from Tickers.csv.
    Legacy format — precedes Phase 1.4. Returns [] on any failure."""
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            tickers = [l.strip().upper() for l in f
                       if l.strip() and not l.strip().startswith('#')]
        out = []; seen = set()
        for t in tickers:
            if t not in seen:
                seen.add(t); out.append(t)
        return out
    except Exception:
        return []


def get_all_symbols():
    """
    Universe source (Phase 1.5 — Layer 1 handoff, union semantics).

    Returns the UNION of (dedup, preserve order):
      1. leaders.csv  — Phase 1.4 automated pick (≤100 Industry + Potential
                        Leaders, refreshed quarterly by the Leader Detector)
      2. Tickers.csv  — manual watchlist. Persists alongside leaders.csv so
                        the user can keep symbols outside the screener's
                        cut — experiments, pre-IPO, thematic plays, or
                        names the rubric is too strict on today.

    Overlap between the two is harmless: dedup below keeps each symbol
    exactly once; leaders.csv ordering wins on ties (so automated picks
    surface first in the UI).

    Fallback: if BOTH files are missing or empty, returns _DEFAULT_SYMBOLS
    (hardcoded 16-ticker safe list) so the server still boots.

    Rollback: delete leaders.csv → only Tickers.csv's manual list surfaces.
    No other Layer 2 code needs to change — scheduler, backtest, and all
    three original tabs route through this function.
    """
    combined = []
    seen = set()

    if os.path.exists(LEADERS_CSV):
        for sym in _read_leaders_csv(LEADERS_CSV):
            if sym not in seen:
                seen.add(sym)
                combined.append(sym)

    if os.path.exists(TICKERS_CSV):
        for sym in _read_tickers_csv(TICKERS_CSV):
            if sym not in seen:
                seen.add(sym)
                combined.append(sym)

    if combined:
        return combined

    return list(_DEFAULT_SYMBOLS)

SYMBOL_UNIVERSE = {"Watchlist": get_all_symbols()}

# =============================================================================
# DATA FETCHING
# =============================================================================
def _ensure_cache_dir(d): os.makedirs(d, exist_ok=True)
def _cache_path(s, d): return os.path.join(d, f"{s}.csv")

def _cache_fresh(symbol, cache_dir, max_age_days=CACHE_DAYS):
    """
    Cache is fresh only if the last data row is from the most recent
    completed trading day. Uses US/Eastern time for market close detection.
    """
    path = _cache_path(symbol, cache_dir)
    if not os.path.exists(path): return False
    if (time.time() - os.path.getmtime(path)) / 86400 >= max_age_days: return False
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.empty: return False
        last = df.index[-1].date()

        # Get current time in US/Eastern (market timezone)
        import zoneinfo
        try:
            est_now = datetime.now(zoneinfo.ZoneInfo("US/Eastern"))
        except Exception:
            # Fallback: assume Pacific time (Pasadena) = EST - 3 hours
            est_now = datetime.now() + timedelta(hours=3)
        est_today = est_now.date()

        # Market closes at 4 PM EST. Yahoo posts data within ~1-3 min.
        market_closed = est_now.hour >= 16

        if est_today.weekday() == 0:  # Monday
            exp = est_today - timedelta(days=3) if not market_closed else est_today
        elif est_today.weekday() == 6:  # Sunday
            exp = est_today - timedelta(days=2)
        elif est_today.weekday() == 5:  # Saturday
            exp = est_today - timedelta(days=1)
        else:  # Tue-Fri
            if market_closed:
                exp = est_today  # today's data should be available
            else:
                exp = est_today - timedelta(days=1)
                if exp.weekday() == 6: exp -= timedelta(days=2)
                elif exp.weekday() == 5: exp -= timedelta(days=1)

        return last >= exp
    except Exception: return False

def _download_batch(syms, start):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return yf.download(syms, start=start, interval='1d', auto_adjust=True,
                               group_by='ticker', progress=False, threads=False)
        except Exception as e:
            w = BACKOFF_BASE ** attempt + random.uniform(0, 1)
            print(f"  [retry {attempt}/{MAX_RETRIES}] {e}. Waiting {w:.1f}s"); time.sleep(w)
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries.")

def fetch_stock_data(symbols, start='2010-01-01', cache_dir=None):
    cache_dir = cache_dir or CACHE_DIR; _ensure_cache_dir(cache_dir)
    raw, to_dl = {}, []
    for s in symbols:
        if _cache_fresh(s, cache_dir):
            raw[s] = pd.read_csv(_cache_path(s, cache_dir), index_col=0, parse_dates=True)
        else: to_dl.append(s)
    if to_dl:
        print(f"Fetching {len(to_dl)} symbols..."); time.sleep(FETCH_DELAY_SEC)
        batch = _download_batch(to_dl, start)
        for s in to_dl:
            try:
                sdf = batch[s].copy() if len(to_dl) > 1 else batch.copy()
                if isinstance(sdf.columns, pd.MultiIndex): sdf = sdf.droplevel(0, axis=1)
                sdf.dropna(subset=['Close'], inplace=True)
                if sdf.empty: continue
                sdf.to_csv(_cache_path(s, cache_dir)); raw[s] = sdf
            except (KeyError, TypeError): print(f"  {s} not found, skipping.")
    return raw

# =============================================================================
# SVR
# =============================================================================
def _fetch_svr(symbol):
    try:
        tk = yf.Ticker(symbol); info = tk.info or {}
        mc = info.get('marketCap')
        # Sector / Industry (stocks) or quote type (ETF)
        sector = info.get('sector')
        industry = info.get('industry')
        quote_type = info.get('quoteType')  # "ETF", "EQUITY", "MUTUALFUND", etc.
        if not mc: return None, None, None, sector, industry, quote_type
        qrev = None
        for attr in ('quarterly_income_stmt', 'quarterly_financials'):
            stmt = getattr(tk, attr, None)
            if stmt is not None and not stmt.empty:
                for lab in ('Total Revenue','Revenue','TotalRevenue','Operating Revenue'):
                    if lab in stmt.index:
                        v = stmt.loc[lab].dropna()
                        if len(v) > 0: qrev = float(v.iloc[0]); break
            if qrev: break
        if not qrev or qrev <= 0: return None, mc, None, sector, industry, quote_type
        return round(mc / (qrev * 4), 2), mc, round(qrev, 0), sector, industry, quote_type
    except Exception as e:
        print(f"  [SVR] {symbol}: {e}"); return None, None, None, None, None, None

# =============================================================================
# V2 FEATURES (13)
# =============================================================================
V2_FEATURE_COLS = ['Dist_SMA50','Dist_SMA200','Dist_EMA50','Dist_EMA200','RSI','BB_Position','BB_Width',
                   'Return_1d','Return_5d','Return_20d','RVol_20d','SMA_Cross','EMA_Cross']

def engineer_features_v2(df):
    c = df['Close'].squeeze()
    s50 = SMAIndicator(c,50).sma_indicator(); s200 = SMAIndicator(c,200).sma_indicator()
    e50 = EMAIndicator(c,50).ema_indicator(); e200 = EMAIndicator(c,200).ema_indicator()
    rsi = RSIIndicator(c,14).rsi(); bb = BollingerBands(c,20,2)
    bh, bl = bb.bollinger_hband(), bb.bollinger_lband()
    df['Dist_SMA50']=(c-s50)/s50; df['Dist_SMA200']=(c-s200)/s200
    df['Dist_EMA50']=(c-e50)/e50; df['Dist_EMA200']=(c-e200)/e200
    bb_w=bh-bl; df['RSI']=rsi
    df['BB_Position']=np.where(bb_w.abs()>1e-10,(c-bl)/bb_w,0.5); df['BB_Width']=bb_w/c
    df['Return_1d']=c.pct_change(1); df['Return_5d']=c.pct_change(5); df['Return_20d']=c.pct_change(20)
    df['RVol_20d']=df['Return_1d'].rolling(20).std()
    df['SMA_Cross']=(s50>s200).astype(float); df['EMA_Cross']=(e50>e200).astype(float)
    df['Target_Return']=c.pct_change(1).shift(-1); df['Volatility_20d']=df['RVol_20d']
    return df

# =============================================================================
# V3 FEATURES (22 — trimmed from 37, removed redundant/noisy features)
# =============================================================================
V3_FEATURE_COLS = [
    # Core price structure (9)
    'Dist_SMA50','Dist_SMA200','RSI','BB_Position',
    'Return_1d','Return_5d','Return_20d','RVol_20d','SMA_Cross',
    # Volume (3)
    'Volume_Ratio_20d','OBV_Slope_10d','Volume_Zscore_20d',
    # Multi-timeframe momentum (2)
    'ROC_10d','ROC_60d',
    # Volatility (2)
    'ATR_Norm','GK_Vol_20d',
    # Mean reversion (1)
    'Zscore_50d',
    # Trend strength (2)
    'ADX_14','MACD_Hist_Norm',
    # Lagged (3)
    'RSI_Lag1','Return_1d_Lag1','BB_Position_Lag1',
]

def engineer_features_v3(df):
    """22 features only — trimmed from 37 (removed redundant/noisy features)."""
    c=df['Close'].squeeze(); h=df['High'].squeeze(); l=df['Low'].squeeze()
    o=df['Open'].squeeze(); v=df['Volume'].squeeze().astype(float)
    # Technical indicators
    s50=SMAIndicator(c,50).sma_indicator(); s200=SMAIndicator(c,200).sma_indicator()
    rsi=RSIIndicator(c,14).rsi()
    bb=BollingerBands(c,20,2); bh,bl=bb.bollinger_hband(),bb.bollinger_lband()
    # Core price structure (9)
    df['Dist_SMA50']=(c-s50)/s50; df['Dist_SMA200']=(c-s200)/s200
    bb_w=bh-bl; df['RSI']=rsi; df['BB_Position']=np.where(bb_w.abs()>1e-10,(c-bl)/bb_w,0.5)
    df['Return_1d']=c.pct_change(1); df['Return_5d']=c.pct_change(5); df['Return_20d']=c.pct_change(20)
    df['RVol_20d']=df['Return_1d'].rolling(20).std()
    df['SMA_Cross']=(s50>s200).astype(float)
    # Volume (3)
    vs20=v.rolling(20).mean(); df['Volume_Ratio_20d']=v/vs20
    obv=OnBalanceVolumeIndicator(c,v).on_balance_volume()
    obv_s=obv.rolling(10).apply(lambda x:np.polyfit(np.arange(len(x)),x,1)[0] if len(x)==10 else 0,raw=True)
    df['OBV_Slope_10d']=obv_s/(v.rolling(10).mean()+1e-10)
    df['Volume_Zscore_20d']=(v-vs20)/(v.rolling(20).std()+1e-10)
    # Multi-timeframe momentum (2)
    df['ROC_10d']=ROCIndicator(c,10).roc(); df['ROC_60d']=ROCIndicator(c,60).roc()
    # Volatility (2)
    atr=AverageTrueRange(h,l,c,14).average_true_range(); df['ATR_Norm']=atr/c
    lhl=np.log(h/l)**2; lco=np.log(c/o)**2
    df['GK_Vol_20d']=(0.5*lhl-(2*np.log(2)-1)*lco).rolling(20).mean()
    # Mean reversion (1)
    df['Zscore_50d']=(c-s50)/(c.rolling(50).std()+1e-10)
    # Trend strength (2)
    df['ADX_14']=ADXIndicator(h,l,c,14).adx()
    macd=MACD(c,26,12,9); df['MACD_Hist_Norm']=macd.macd_diff()/c
    # Lagged (3)
    df['RSI_Lag1']=rsi.shift(1)
    df['Return_1d_Lag1']=df['Return_1d'].shift(1)
    df['BB_Position_Lag1']=df['BB_Position'].shift(1)
    # Target
    df['Target_Return']=c.pct_change(1).shift(-1); df['Volatility_20d']=df['RVol_20d']
    return df

# =============================================================================
# V2 MODELS
# =============================================================================
def train_rf_v2(X,y):
    m=RandomForestRegressor(n_estimators=200,max_depth=6,min_samples_leaf=10,max_features='sqrt',random_state=42,n_jobs=1)
    m.fit(X,y); return m
def train_xgb_v2(X,y):
    m=xgb.XGBRegressor(objective='reg:squarederror',n_estimators=200,max_depth=4,learning_rate=0.05,
                        subsample=0.8,colsample_bytree=0.8,reg_alpha=0.5,reg_lambda=2.0,random_state=42,n_jobs=1)
    m.fit(X,y,verbose=False); return m
def predict_v2(models,X):
    return np.clip(0.8*models[0].predict(X)+0.2*models[1].predict(X),-0.08,0.08)

# =============================================================================
# V3 MODELS (Stacking Ensemble)
# =============================================================================
def train_lgbm_v3(Xt,yt,Xv,yv):
    m=lgb.LGBMRegressor(n_estimators=1000,max_depth=5,learning_rate=0.03,subsample=0.7,colsample_bytree=0.7,
                         reg_alpha=0.3,reg_lambda=1.5,min_child_samples=20,random_state=42,verbose=-1,n_jobs=1)
    m.fit(Xt,yt,eval_set=[(Xv,yv)],callbacks=[lgb.early_stopping(50,verbose=False)]); return m

def train_xgb_v3(Xt,yt,Xv,yv):
    m=xgb.XGBRegressor(objective='reg:squarederror',n_estimators=1000,max_depth=4,learning_rate=0.03,
                        subsample=0.7,colsample_bytree=0.7,reg_alpha=0.5,reg_lambda=2.0,
                        random_state=42,n_jobs=1,early_stopping_rounds=50)
    m.fit(Xt,yt,eval_set=[(Xv,yv)],verbose=False); return m

def train_rf_v3(X,y):
    m=RandomForestRegressor(n_estimators=300,max_depth=8,min_samples_leaf=15,max_features=0.5,random_state=42,n_jobs=1)
    m.fit(X,y); return m

def build_stacking_ensemble(X_train,y_train,X_val,y_val):
    """Build ensemble with OOF inverse-MAE weighted average.
    Replaces Ridge meta-learner which compressed predictions to near-zero."""
    tscv=TimeSeriesSplit(n_splits=5); oof=np.zeros((len(X_train),3))
    for _,(ti,vi) in enumerate(tscv.split(X_train)):
        Xft,yft=X_train[ti],y_train[ti]; Xfv,yfv=X_train[vi],y_train[vi]
        oof[vi,0]=train_lgbm_v3(Xft,yft,Xfv,yfv).predict(Xfv)
        oof[vi,1]=train_xgb_v3(Xft,yft,Xfv,yfv).predict(Xfv)
        oof[vi,2]=train_rf_v3(Xft,yft).predict(Xfv)
    # Inverse-MAE weighting (proven better than Ridge in backtest)
    mask=np.any(oof!=0,axis=1)
    oof_valid=oof[mask]; y_valid=y_train[mask]
    mae_per_model=np.array([np.mean(np.abs(oof_valid[:,j]-y_valid)) for j in range(3)])
    inv_mae=1.0/(mae_per_model+0.005)  # larger epsilon prevents extreme weight imbalance
    weights=inv_mae/inv_mae.sum()
    # Retrain final models on full training data
    fl=train_lgbm_v3(X_train,y_train,X_val,y_val)
    fx=train_xgb_v3(X_train,y_train,X_val,y_val)
    fr=train_rf_v3(X_train,y_train)
    return {'lgbm':fl,'xgb':fx,'rf':fr,'weights':weights}

def predict_v3(ens,X):
    """Weighted average prediction — no clipping, preserves full signal range."""
    w=ens['weights']
    return w[0]*ens['lgbm'].predict(X)+w[1]*ens['xgb'].predict(X)+w[2]*ens['rf'].predict(X)

# =============================================================================
# PREDICT TICKER (used by dashboard + CLI)
# =============================================================================
def predict_ticker(symbol,cache_dir=None,verbose=True,version=None,strategy=None,**kwargs):
    cache_dir=cache_dir or CACHE_DIR; ver=version or MODEL_VERSION
    strat = get_strategy_mode(symbol, strategy)
    raw=fetch_stock_data([symbol],cache_dir=cache_dir)
    if symbol not in raw or raw[symbol].empty: return {"symbol":symbol,"error":"No data available"}

    df=raw[symbol].copy()
    if ver=='v3' and HAS_LGBM:
        df=engineer_features_v3(df); fcols=V3_FEATURE_COLS
    else:
        df=engineer_features_v2(df); fcols=V2_FEATURE_COLS; ver='v2'

    latest_row=df.iloc[-1:]; dc=df.dropna(subset=['Target_Return']+fcols).copy()
    if len(dc)<100: return {"symbol":symbol,"error":f"Insufficient data ({len(dc)} rows, need 100+)"}

    aX=dc[fcols].values; ay=dc['Target_Return'].values; te=len(aX)
    vs=int(te*0.85); Xtr,ytr=aX[:vs],ay[:vs]; Xvl,yvl=aX[vs:],ay[vs:]

    scaler=StandardScaler(); scaler.fit(Xtr)  # fit on TRAIN only — no validation leakage
    Xtr_s=scaler.transform(Xtr); Xvl_s=scaler.transform(Xvl)

    if ver=='v3':
        model=build_stacking_ensemble(Xtr_s,ytr,Xvl_s,yvl); pf=predict_v3
    else:
        rf=train_rf_v2(scaler.transform(aX[:te]),ay[:te])
        xm=train_xgb_v2(scaler.transform(aX[:te]),ay[:te])
        model=(rf,xm); pf=predict_v2

    # Eval on validation (raw predictions, no rescaling needed with new ensemble)
    yp=pf(model,Xvl_s)
    mae_r=mean_absolute_error(yvl,yp); rmse_r=np.sqrt(mean_squared_error(yvl,yp))
    dir_acc=np.mean(np.sign(yp)==np.sign(yvl))*100

    # Build prediction history for Z-score (using recent validation predictions)
    pred_history=list(yp[-ZSCORE_LOOKBACK:])

    # Quick backtest on validation via BacktestEngine (C-3 Phase 3).
    # Engine enforces MIN_ZSCORE_SAMPLES uniformly; seed_from_validation=False
    # mirrors the old inline loop's head-seeded pred_history so steps from
    # index seed_n onward are byte-identical. Leading seed_n rows (old loop's
    # "seed phase") are trimmed here to preserve predict_ticker's return shape.
    from backtest_engine import BacktestConfig, BacktestEngine, full_signal as _bt_full, buy_only as _bt_buyonly
    seed_n=min(20,max(5,len(Xvl)//3))  # adaptive seed for short-history tickers
    _bt_cfg=BacktestConfig(symbol=symbol,strategy_name=strat,initial_cash=10000.0,
                           threshold=THRESHOLD,zscore_lookback=ZSCORE_LOOKBACK,
                           min_zscore_samples=MIN_ZSCORE_SAMPLES,retrain_freq_days=None,
                           min_train_days=MIN_TRAIN_DAYS,seed_from_validation=False,
                           random_state=42,ensemble_builder='oof',feature_version=ver)
    _bt_res=BacktestEngine(_bt_cfg,dc).run(_bt_buyonly if strat=='buy_only' else _bt_full)
    btp=_bt_res.portfolio_curve[seed_n:]  # drop leading seed-phase rows
    bt_ret=(btp[-1]/10000-1)*100 if btp else 0
    tc=dc['Close'].values[vs+seed_n:vs+seed_n+len(btp)]
    bnh_ret=(tc[-1]/tc[0]-1)*100 if len(tc)>0 else 0
    # Strategy stats
    bd=np.diff(btp)/np.array(btp[:-1]) if len(btp)>1 else [0]
    bt_sh=float(np.mean(bd)/np.std(bd)*np.sqrt(252)) if np.std(bd)>0 else 0
    bpk=np.maximum.accumulate(btp) if btp else [1]
    bt_dd=float(((np.array(btp)-bpk)/bpk).min())*100
    # Buy & Hold stats for the same validation window (benchmark)
    if len(tc)>1:
        bnh_port=10000.0*(tc/tc[0])
        bnh_dr=np.diff(bnh_port)/bnh_port[:-1]
        bnh_sh=float(np.mean(bnh_dr)/np.std(bnh_dr)*np.sqrt(252)) if np.std(bnh_dr)>0 else 0
        bnh_pk=np.maximum.accumulate(bnh_port)
        bnh_dd=float(((bnh_port-bnh_pk)/bnh_pk).min())*100
    else:
        bnh_sh=0; bnh_dd=0

    # Predict next day
    lf=latest_row[fcols].values
    if np.isnan(lf).any(): lf=aX[-1:].copy()
    ls=scaler.transform(lf.reshape(1,-1))
    rp=pf(model,ls)[0]
    cp_price=float(df['Close'].iloc[-1]); pp=cp_price*(1+rp)

    # Compute Z-score of today's prediction vs recent history
    pred_history.append(rp)
    if len(pred_history)>ZSCORE_LOOKBACK: pred_history=pred_history[-ZSCORE_LOOKBACK:]
    hist=np.array(pred_history[:-1])
    mu,sigma=np.mean(hist),np.std(hist)
    z_score=(rp-mu)/sigma if sigma>1e-10 else 0.0

    # Per-model breakdown
    if ver=='v3':
        mpreds={"LGBM":round(cp_price*(1+float(model['lgbm'].predict(ls)[0])),2),
                "XGB":round(cp_price*(1+float(model['xgb'].predict(ls)[0])),2),
                "RF":round(cp_price*(1+float(model['rf'].predict(ls)[0])),2)}
    else:
        mpreds={"RF":round(cp_price*(1+float(model[0].predict(ls)[0])),2),
                "XGB":round(cp_price*(1+float(model[1].predict(ls)[0])),2)}

    # Signal: Z-score threshold + SVR filter + strategy mode
    svr,mc,qr,sector,industry,quote_type=_fetch_svr(symbol)
    pp_pct=rp*100
    sok=(svr is None)or(svr<=7); ste=(svr is not None)and(svr>=15)

    if strat == 'buy_only':
        # Buy-Only: Z-score triggers BUY, only extreme SVR overvaluation triggers SELL
        # Backtest-proven: sell signals hurt individual stocks (Sharpe -0.14 to -0.35)
        if z_score>=THRESHOLD and sok: sig="BUY"
        elif ste: sig="SELL"  # only SVR overvaluation warning
        else: sig="HOLD"
        sig_rules=f"BUY: Z>={THRESHOLD} & SVR<=7 | SELL: SVR>=15 only (buy-only mode)"
    else:
        # Full Signal: BUY and SELL from Z-score + SVR
        # Backtest-proven: sell signals help ETFs (Sharpe +0.06 to +0.20)
        if z_score>=THRESHOLD and sok: sig="BUY"
        elif z_score<=-THRESHOLD or ste: sig="SELL"
        else: sig="HOLD"
        sig_rules=f"BUY: Z>={THRESHOLD} & SVR<=7 | SELL: Z<=-{THRESHOLD} or SVR>=15"

    vl="Pro (Stacking)" if ver=='v3' else "Lite (RF+XGB)"
    strat_label="Full Signal" if strat=='full' else "Buy-Only"
    result={"symbol":symbol,"current_price":round(cp_price,2),"predicted_price":round(float(pp),2),
            "pct_change":round(float(rp)*100,2),"z_score":round(float(z_score),2),"signal":sig,
            "signal_rules":sig_rules,"strategy_mode":strat,"strategy_label":strat_label,
            "model_version":vl,"model_predictions":mpreds,
            "svr":svr,"market_cap":mc,"quarterly_revenue":qr,
            "sector":sector,"industry":industry,"quote_type":quote_type,
            "backtest_mae_pct":round(float(mae_r)*100,3),"backtest_rmse_pct":round(float(rmse_r)*100,3),
            "direction_accuracy":round(float(dir_acc),1),
            "backtest_mae":round(float(mae_r)*cp_price,2),"backtest_rmse":round(float(rmse_r)*cp_price,2),
            "data_points":len(dc),"train_window":te,"train_window_setting":"All data",
            "last_date":str(df.index[-1].date()),
            "backtest":{"strategy_return":round(bt_ret,2),"buyhold_return":round(bnh_ret,2),
                        "sharpe":round(bt_sh,2),"max_drawdown":round(bt_dd,1),
                        "bnh_sharpe":round(bnh_sh,2),"bnh_max_drawdown":round(bnh_dd,1),
                        "test_days":len(btp)}}
    if verbose:
        svr_s=f"{svr:.1f}x" if svr else "N/A"
        print(f"\n{'='*55}\n  {symbol}  ({vl} | {strat_label})\n{'='*55}")
        print(f"  Current:   ${cp_price:.2f}  |  Predicted: ${pp:.2f} ({pp_pct:+.2f}%)")
        print(f"  Z-score:   {z_score:+.2f}  |  Signal: {sig} (SVR {svr_s})")
        print(f"  Strategy:  {strat_label} {'(ETF)' if strat=='full' else '(Stock)'}")
        print(f"  Dir Acc:   {dir_acc:.1f}%  |  Backtest: {bt_ret:+.1f}% vs B&H {bnh_ret:+.1f}%")
        print(f"  Models:    {mpreds}")
        print(f"  Data:      {len(dc)} rows through {df.index[-1].date()}")
    return result

# =============================================================================
# COMPARE BOTH MODELS (single ticker)
# =============================================================================
def predict_ticker_compare(symbol, cache_dir=None, verbose=False, strategy=None):
    """Run both V2 and V3 on a single ticker and return combined result."""
    cache_dir = cache_dir or CACHE_DIR
    strat = get_strategy_mode(symbol, strategy)
    r_v2 = predict_ticker(symbol, cache_dir=cache_dir, verbose=False, version='v2', strategy=strat)
    r_v3 = predict_ticker(symbol, cache_dir=cache_dir, verbose=False, version='v3', strategy=strat) if HAS_LGBM else None

    if 'error' in r_v2 and (r_v3 is None or 'error' in r_v3):
        return r_v2  # both failed

    # Consensus signal
    sig_v2 = r_v2.get('signal', 'HOLD') if 'error' not in r_v2 else 'HOLD'
    sig_v3 = r_v3.get('signal', 'HOLD') if r_v3 and 'error' not in r_v3 else 'HOLD'
    if sig_v2 == sig_v3:
        consensus = sig_v2
        confidence = 'HIGH' if sig_v2 != 'HOLD' else 'NEUTRAL'
    elif 'HOLD' in (sig_v2, sig_v3):
        consensus = sig_v2 if sig_v2 != 'HOLD' else sig_v3
        confidence = 'MEDIUM'
    else:
        consensus = 'HOLD'
        confidence = 'CONFLICT'

    strat_label = "Full Signal" if strat=='full' else "Buy-Only"
    result = {
        'symbol': symbol,
        'current_price': r_v2.get('current_price') or (r_v3 or {}).get('current_price'),
        'last_date': r_v2.get('last_date') or (r_v3 or {}).get('last_date'),
        'svr': r_v2.get('svr'),
        'market_cap': r_v2.get('market_cap'),
        'quarterly_revenue': r_v2.get('quarterly_revenue'),
        'sector': r_v2.get('sector') or (r_v3 or {}).get('sector'),
        'industry': r_v2.get('industry') or (r_v3 or {}).get('industry'),
        'quote_type': r_v2.get('quote_type') or (r_v3 or {}).get('quote_type'),
        'consensus_signal': consensus,
        'confidence': confidence,
        'strategy_mode': strat,
        'strategy_label': strat_label,
        'v2': r_v2 if 'error' not in r_v2 else None,
        'v3': r_v3 if r_v3 and 'error' not in r_v3 else None,
    }
    if verbose:
        cp = result['current_price']
        p2 = r_v2.get('predicted_price', '?') if 'error' not in r_v2 else '?'
        p3 = (r_v3 or {}).get('predicted_price', '?') if r_v3 and 'error' not in r_v3 else '?'
        c2 = r_v2.get('pct_change', 0) if 'error' not in r_v2 else 0
        c3 = (r_v3 or {}).get('pct_change', 0) if r_v3 and 'error' not in r_v3 else 0
        print(f"\n{'='*60}\n  {symbol}  COMPARE  ({strat_label})\n{'='*60}")
        print(f"  Current: ${cp}  |  Lite: ${p2} ({c2:+.2f}%)  |  Pro: ${p3} ({c3:+.2f}%)")
        print(f"  Lite Signal: {sig_v2}  |  Pro Signal: {sig_v3}  |  Consensus: {consensus} ({confidence})")
        print(f"  Strategy: {strat_label} {'(ETF — SELL signals active)' if strat=='full' else '(Stock — BUY only, hold)'}")
    return result


# =============================================================================
# DUAL-MODEL DAILY REPORT
# =============================================================================
def daily_scan_both(symbols=None, cache_dir=None, top_n=10):
    """Run both V2 and V3 on all symbols and produce a unified report."""
    symbols = symbols or get_all_symbols()
    cache_dir = cache_dir or CACHE_DIR
    results = []
    print(f"\n{'#'*60}\n  DUAL-MODEL SCAN — {datetime.now():%Y-%m-%d %H:%M} | {len(symbols)} symbols\n{'#'*60}\n")

    for i, s in enumerate(symbols):
        try:
            r = predict_ticker_compare(s, cache_dir=cache_dir, verbose=False)
            if 'error' not in r:
                results.append(r)
                sig = r.get('consensus_signal', '?')
                conf = r.get('confidence', '?')
                v2c = r['v2']['pct_change'] if r.get('v2') else 0
                v3c = r['v3']['pct_change'] if r.get('v3') else 0
                print(f"  [{i+1:3d}/{len(symbols)}] {s:<6} Lite:{v2c:+6.2f}% Pro:{v3c:+6.2f}% → {sig} ({conf})")
            else:
                print(f"  [{i+1:3d}/{len(symbols)}] {s:<6} {r.get('error','Unknown error')}")
        except Exception as e:
            print(f"  [{i+1:3d}/{len(symbols)}] {s:<6} ERROR: {e}")

    if not results:
        print("No results.")
        return []

    # Sort by average predicted change
    for r in results:
        v2c = r['v2']['pct_change'] if r.get('v2') else 0
        v3c = r['v3']['pct_change'] if r.get('v3') else 0
        r['avg_pct_change'] = round((v2c + v3c) / 2, 2)
    results.sort(key=lambda x: x['avg_pct_change'], reverse=True)

    # Summary stats
    buy_count = sum(1 for r in results if r['consensus_signal'] == 'BUY')
    sell_count = sum(1 for r in results if r['consensus_signal'] == 'SELL')
    hold_count = sum(1 for r in results if r['consensus_signal'] == 'HOLD')
    high_conf = sum(1 for r in results if r['confidence'] == 'HIGH')
    conflict_count = sum(1 for r in results if r['confidence'] == 'CONFLICT')

    summary = {
        'generated_at': datetime.now().isoformat(),
        'total_symbols': len(results),
        'consensus_buy': buy_count,
        'consensus_sell': sell_count,
        'consensus_hold': hold_count,
        'high_confidence': high_conf,
        'model_conflict': conflict_count,
        'market_sentiment': 'BULLISH' if buy_count > sell_count * 1.5 else ('BEARISH' if sell_count > buy_count * 1.5 else 'MIXED'),
    }

    # Save to disk
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    report_path = os.path.join(cache_dir, f"dual_report_{ts}.json")
    _ensure_cache_dir(cache_dir)
    import json as _json
    with open(report_path, 'w') as f:
        _json.dump({'summary': summary, 'data': results}, f, indent=2, default=str)
    print(f"\n  Report saved: {report_path}")

    return {'summary': summary, 'data': results}


# =============================================================================
# DAILY SCANNER (single model — legacy)
# =============================================================================
def daily_scan(symbols=None,cache_dir=None,top_n=10):
    symbols=symbols or get_all_symbols(); cache_dir=cache_dir or CACHE_DIR; results=[]
    print(f"\n{'#'*60}\n  DAILY SCAN — {datetime.now():%Y-%m-%d %H:%M} | {MODEL_VERSION.upper()} | {len(symbols)} symbols\n{'#'*60}\n")
    for i,s in enumerate(symbols):
        try:
            r=predict_ticker(s,cache_dir=cache_dir,verbose=False)
            if "error" not in r: results.append(r); st=f"{r['signal']:>4} {r['pct_change']:+6.2f}%"
            else: st=r['error']
            print(f"  [{i+1:3d}/{len(symbols)}] {s:<6} {st}")
        except Exception as e: print(f"  [{i+1:3d}/{len(symbols)}] {s:<6} ERROR: {e}")
    if not results: print("No results."); return pd.DataFrame()
    df=pd.DataFrame(results).sort_values('pct_change',ascending=False).reset_index(drop=True)
    for title,rows in [("GAINERS",df.head(top_n)),("DECLINERS",df.tail(top_n).iloc[::-1])]:
        print(f"\n{'='*72}\n  TOP {top_n} {title}\n{'='*72}")
        for _,r in rows.iterrows():
            sv=f"{r['svr']:.1f}x" if r.get('svr') else "  N/A"
            print(f"  {r['symbol']:<6} ${r['current_price']:>8.2f} -> ${r['predicted_price']:>8.2f} ({r['pct_change']:+.2f}%) {r['signal']:>4} {sv:>6}")
    ts=datetime.now().strftime('%Y%m%d_%H%M')
    df.to_csv(os.path.join(cache_dir,f"daily_scan_{ts}.csv"),index=False)
    df.to_json(os.path.join(cache_dir,f"daily_scan_{ts}.json"),orient='records',indent=2)
    return df

# =============================================================================
# BACKTEST
# =============================================================================
def backtest_symbol(symbol,cache_dir=None,version=None,initial_cash=10000,strategy=None):
    """Walk-forward backtest with Z-score signal strategy (respects strategy mode).

    Routed through ``backtest_engine.BacktestEngine`` (C-3 Phase 4a). Preserves
    the legacy return type (raw numpy portfolio curve) and the two-line console
    summary. The engine uses ``ensemble_builder='oof'`` to match the
    pre-refactor baseline for this caller (``build_stacking_ensemble``)."""
    cache_dir=cache_dir or CACHE_DIR; ver=version or MODEL_VERSION
    strat = get_strategy_mode(symbol, strategy)
    raw=fetch_stock_data([symbol],cache_dir=cache_dir)
    if symbol not in raw or raw[symbol].empty: return None
    df=raw[symbol].copy()
    if ver=='v3' and HAS_LGBM:
        df=engineer_features_v3(df); fc=V3_FEATURE_COLS; rf_freq=RETRAIN_FREQ_V3
    else:
        df=engineer_features_v2(df); fc=V2_FEATURE_COLS; rf_freq=RETRAIN_FREQ_V2; ver='v2'
    dc=df.dropna(subset=['Target_Return']+fc).copy()
    # MIN_BACKTEST_DAYS guard lives in the engine (Phase 5 of C-3). We still
    # compute ``bsi`` here because the benchmark buy-and-hold slice below
    # needs it — but the insufficient-data decision belongs to the engine.
    bm=dc.index>=pd.Timestamp(BACKTEST_PREFERRED_START)
    bsi=int(np.argmax(bm)) if bm.any() else 0
    bsi=max(bsi, MIN_TRAIN_DAYS)

    from backtest_engine import BacktestConfig, BacktestEngine, full_signal as _bt_full, buy_only as _bt_buyonly
    cfg=BacktestConfig(symbol=symbol,strategy_name=strat,initial_cash=float(initial_cash),
                       threshold=THRESHOLD,zscore_lookback=ZSCORE_LOOKBACK,
                       min_zscore_samples=MIN_ZSCORE_SAMPLES,retrain_freq_days=int(rf_freq),
                       min_train_days=MIN_TRAIN_DAYS,min_backtest_days=MIN_BACKTEST_DAYS,
                       seed_from_validation=True,random_state=42,
                       ensemble_builder='oof',feature_version=ver)
    _fn=_bt_buyonly if strat=='buy_only' else _bt_full
    try:
        result=BacktestEngine(cfg,dc).run(_fn)
    except ValueError as exc:
        # Legacy contract: insufficient data -> None + console message.
        print(f"Not enough data for backtest ({len(dc)} rows, need {MIN_TRAIN_DAYS}+{MIN_BACKTEST_DAYS}) [{exc}]")
        return None

    port=np.array(result.portfolio_curve)
    # Benchmark: buy-and-hold starting at bsi (the first simulated bar).
    ap=dc['Close'].values.ravel()
    tp=ap[bsi:bsi+len(port)]
    bnh=initial_cash*(tp/tp[0]) if len(tp)>0 else np.array([float(initial_cash)])
    sr=(port[-1]/initial_cash-1)*100 if port.size else 0.0
    br=(bnh[-1]/initial_cash-1)*100 if bnh.size else 0.0

    vl="Pro" if ver=='v3' else "Lite"
    strat_label="Full Signal" if strat=='full' else "Buy-Only"
    print(f"\n{'='*60}\n  BACKTEST: {symbol} ({vl}, {strat_label}, Z-score ±{THRESHOLD}σ)\n{'='*60}")
    print(f"  Period: {len(port)} days | Signals: {result.buys} BUY, {result.sells} SELL, {result.holds} HOLD")
    print(f"  Strategy: {sr:+.1f}% | B&H: {br:+.1f}% | Sharpe: {result.sharpe:.2f} | MaxDD: {result.max_drawdown_pct:.1f}%")
    return port


def backtest_multi_strategy(symbol, cache_dir=None, version=None, initial_cash=10000, progress_cb=None):
    """Walk-forward backtest running BOTH full and buy_only strategies simultaneously.

    Routed through ``backtest_engine.BacktestEngine.run_multi`` (C-3 Phase 4b).
    Preserves the exact legacy return shape consumed by ``api_server._run_backtest_chart``:

        {symbol, version, version_label, period_days, start_date, dates,
         buyhold, full: {portfolio, return_pct, sharpe, max_drawdown, buys, sells},
         buy_only: {portfolio, return_pct, sharpe, max_drawdown, buys, sells},
         buyhold_stats: {return_pct, sharpe, max_drawdown, buys, sells}}

    Engine-only fields (raw_predictions, z_scores, config_hash, sortino, …)
    are intentionally NOT exposed — see tests/unit/test_api_backtest_wire_format.py
    for the regression guard.

    Ensemble-builder change (intentional, C-3 bug fix): pre-refactor this
    function used a val-MAE-weighted "fast" builder (``build_stacking_ensemble_fast``,
    deleted in Phase 5) while ``backtest_symbol`` used ``build_stacking_ensemble``
    (OOF). Now both paths use OOF via the engine default. On MSFT this shifts
    multi_strategy[full] from $87,367.59 -> $92,159.04, now matching
    backtest_symbol. All other tickers agree within 1e-4 between the two
    ensemble choices and remain unchanged.
    """
    cache_dir = cache_dir or CACHE_DIR
    ver = version or MODEL_VERSION
    raw = fetch_stock_data([symbol], cache_dir=cache_dir)
    if symbol not in raw or raw[symbol].empty:
        return None
    df = raw[symbol].copy()
    if ver == 'v3' and HAS_LGBM:
        df = engineer_features_v3(df); fc = V3_FEATURE_COLS; rf_freq = RETRAIN_FREQ_V3
    else:
        df = engineer_features_v2(df); fc = V2_FEATURE_COLS; rf_freq = RETRAIN_FREQ_V2; ver = 'v2'
    dc = df.dropna(subset=['Target_Return'] + fc).copy()
    # MIN_BACKTEST_DAYS guard lives in the engine (Phase 5 of C-3). We still
    # compute ``bsi`` here for the buy-and-hold benchmark slice below; the
    # insufficient-data decision belongs to the engine.
    bm = dc.index >= pd.Timestamp(BACKTEST_PREFERRED_START)
    bsi = int(np.argmax(bm)) if bm.any() else 0
    bsi = max(bsi, MIN_TRAIN_DAYS)

    from backtest_engine import BacktestConfig, BacktestEngine, full_signal as _bt_full, buy_only as _bt_buyonly
    cfg = BacktestConfig(symbol=symbol, strategy_name='full', initial_cash=float(initial_cash),
                         threshold=THRESHOLD, zscore_lookback=ZSCORE_LOOKBACK,
                         min_zscore_samples=MIN_ZSCORE_SAMPLES, retrain_freq_days=int(rf_freq),
                         min_train_days=MIN_TRAIN_DAYS, min_backtest_days=MIN_BACKTEST_DAYS,
                         seed_from_validation=True, random_state=42,
                         ensemble_builder='oof', feature_version=ver)
    try:
        results = BacktestEngine(cfg, dc).run_multi(
            {'full': _bt_full, 'buy_only': _bt_buyonly},
            progress_cb=progress_cb,
        )
    except ValueError as exc:
        # Legacy contract: insufficient data -> None + console message.
        print(f"  [{symbol}] Not enough data for backtest ({len(dc)} rows, need {MIN_TRAIN_DAYS}+{MIN_BACKTEST_DAYS}) [{exc}]")
        return None
    f_res = results['full']
    b_res = results['buy_only']

    f_port = np.array(f_res.portfolio_curve, dtype=float)
    b_port = np.array(b_res.portfolio_curve, dtype=float)
    dates_out = list(f_res.dates)
    ap = dc['Close'].values.ravel()
    tp = ap[bsi:bsi + len(f_port)]
    bnh = initial_cash * (tp / tp[0]) if len(tp) > 0 else np.array([float(initial_cash)])

    def _engine_stats(port, res):
        """Wire-format stats from a BacktestResult. Reuses the engine's own
        sharpe/max_drawdown_pct (no recomputation); only return_pct is
        wire-specific because it's relative to ``initial_cash`` which the
        engine doesn't mirror back.
        """
        if port.size == 0:
            return {'return_pct': 0.0, 'sharpe': 0.0, 'max_drawdown': 0.0,
                    'buys': res.buys, 'sells': res.sells}
        return_pct = (port[-1] / initial_cash - 1) * 100
        return {'return_pct': round(return_pct, 1),
                'sharpe': round(res.sharpe, 2),
                'max_drawdown': round(res.max_drawdown_pct, 1),
                'buys': res.buys, 'sells': res.sells}

    def _buyhold_stats(port):
        """Stats for the buy-and-hold benchmark. The engine doesn't produce a
        ``BacktestResult`` for buyhold (it's a pure price series, not a
        strategy run), so recompute here. Matches the old contract.
        """
        if port.size == 0:
            return {'return_pct': 0.0, 'sharpe': 0.0, 'max_drawdown': 0.0,
                    'buys': 0, 'sells': 0}
        ret = (port[-1] / initial_cash - 1) * 100
        dr = np.diff(port) / port[:-1]
        sharpe = float(np.mean(dr) / np.std(dr) * np.sqrt(252)) if dr.size and np.std(dr) > 0 else 0.0
        pk = np.maximum.accumulate(port)
        mdd = float(((port - pk) / pk).min()) * 100
        return {'return_pct': round(ret, 1), 'sharpe': round(sharpe, 2),
                'max_drawdown': round(mdd, 1), 'buys': 0, 'sells': 0}

    ver_label = "Pro" if ver == 'v3' else "Lite"
    start_date = dates_out[0] if dates_out else None
    print(f"  [{ver_label}] Multi-strategy backtest done — {len(f_port)} days from {start_date}")
    return {
        'symbol': symbol, 'version': ver, 'version_label': ver_label,
        'period_days': len(f_port), 'start_date': start_date, 'dates': dates_out,
        'buyhold': [round(v, 2) for v in bnh.tolist()],
        'full': {'portfolio': [round(v, 2) for v in f_port.tolist()],
                 **_engine_stats(f_port, f_res)},
        'buy_only': {'portfolio': [round(v, 2) for v in b_port.tolist()],
                     **_engine_stats(b_port, b_res)},
        'buyhold_stats': _buyhold_stats(np.asarray(bnh, dtype=float)),
    }


# =============================================================================
# CLI
# =============================================================================
def main():
    p=argparse.ArgumentParser(description="Quantfolio ML Prediction Engine")
    p.add_argument('--ticker','-t',type=str); p.add_argument('--report','-r',action='store_true')
    p.add_argument('--backtest','-b',type=str); p.add_argument('--version','-v',type=str,choices=['v2','v3'])
    p.add_argument('--strategy','-s',type=str,choices=['auto','full','buy_only'],default='auto',
                   help='Signal strategy: auto (ETF→full, stock→buy_only), full (BUY+SELL), buy_only (BUY only)')
    p.add_argument('--cache-dir',type=str,default=CACHE_DIR); p.add_argument('--top',type=int,default=10)
    a=p.parse_args()
    if a.ticker: predict_ticker(a.ticker.upper(),cache_dir=a.cache_dir,version=a.version,strategy=a.strategy)
    elif a.backtest: backtest_symbol(a.backtest.upper(),cache_dir=a.cache_dir,version=a.version,strategy=a.strategy)
    elif a.report: daily_scan(cache_dir=a.cache_dir,top_n=a.top)
    else: daily_scan(cache_dir=a.cache_dir,top_n=a.top)

if __name__=='__main__': main()
