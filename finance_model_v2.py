"""
Finance Model V4 — Stacking Ensemble with Prediction Rescaling
===============================================================
Primary: V3 Stacking (LightGBM + XGBoost + RF -> Ridge meta), 37 features
Fallback: V2 (RF 80% + XGB 20%), 13 features — if LightGBM unavailable

Key innovations from your V4 backtest:
  - Prediction rescaling: raw preds scaled to match actual return std
  - ±2% threshold on rescaled predictions
  - SVR valuation filter: BUY requires SVR<=7, SELL if SVR>=15
  - Walk-forward with quarterly retrain (V3) or 20-day retrain (V2)

Usage:
  python finance_model_v2.py --ticker AAPL
  python finance_model_v2.py --backtest SPY
  python finance_model_v2.py --backtest SPY --version v2
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
from sklearn.linear_model import Ridge
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
DEFAULT_CACHE_DIR = r"C:\Users\xkxuq\Documents\Starup\Finance\data_cache"
CACHE_DIR = os.environ.get("FINANCE_CACHE_DIR", DEFAULT_CACHE_DIR)
CACHE_DAYS = 1
FETCH_DELAY_SEC = 1.5
MAX_RETRIES = 5
BACKOFF_BASE = 2
THRESHOLD = 0.02
RETRAIN_FREQ_V2 = 20
RETRAIN_FREQ_V3 = 63
MODEL_VERSION = 'v3' if HAS_LGBM else 'v2'

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TICKERS_CSV = os.path.join(SCRIPT_DIR, "Tickers.csv")
_DEFAULT_SYMBOLS = ["SPY","QQQ","AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","AMD","AVGO","JPM","UNH","XOM","TLT","GLD"]

def get_all_symbols():
    if os.path.exists(TICKERS_CSV):
        try:
            with open(TICKERS_CSV, 'r', encoding='utf-8-sig') as f:
                tickers = [l.strip().upper() for l in f if l.strip() and not l.strip().startswith('#')]
            seen = set(); out = []
            for t in tickers:
                if t not in seen: seen.add(t); out.append(t)
            if out: return out
        except Exception: pass
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

        # Market closes at 4 PM EST. Data available ~4:30 PM EST.
        market_closed = est_now.hour >= 17  # 5 PM EST to be safe

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
        if not mc: return None, None, None
        qrev = None
        for attr in ('quarterly_income_stmt', 'quarterly_financials'):
            stmt = getattr(tk, attr, None)
            if stmt is not None and not stmt.empty:
                for lab in ('Total Revenue','Revenue','TotalRevenue','Operating Revenue'):
                    if lab in stmt.index:
                        v = stmt.loc[lab].dropna()
                        if len(v) > 0: qrev = float(v.iloc[0]); break
            if qrev: break
        if not qrev or qrev <= 0: return None, mc, None
        return round(mc / (qrev * 4), 2), mc, round(qrev, 0)
    except Exception as e:
        print(f"  [SVR] {symbol}: {e}"); return None, None, None

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
    df['RSI']=rsi; df['BB_Position']=(c-bl)/(bh-bl); df['BB_Width']=(bh-bl)/c
    df['Return_1d']=c.pct_change(1); df['Return_5d']=c.pct_change(5); df['Return_20d']=c.pct_change(20)
    df['RVol_20d']=df['Return_1d'].rolling(20).std()
    df['SMA_Cross']=(s50>s200).astype(float); df['EMA_Cross']=(e50>e200).astype(float)
    df['Target_Return']=c.pct_change(1).shift(-1); df['Volatility_20d']=df['RVol_20d']
    return df

# =============================================================================
# V3 FEATURES (37)
# =============================================================================
V3_FEATURE_COLS = [
    'Dist_SMA50','Dist_SMA200','Dist_EMA50','Dist_EMA200','RSI','BB_Position','BB_Width',
    'Return_1d','Return_5d','Return_20d','RVol_20d','SMA_Cross','EMA_Cross',
    'Volume_Ratio_20d','OBV_Slope_10d','Volume_Price_Div','Volume_Zscore_20d',
    'ROC_3d','ROC_10d','ROC_20d','ROC_60d','ATR_Norm','GK_Vol_20d','Intraday_Range',
    'Zscore_20d','Zscore_50d','ADX_14','MACD_Hist_Norm',
    'Day_of_Week','Month','Quarter_End',
    'RSI_Lag1','RSI_Lag2','Return_1d_Lag1','Return_1d_Lag2','BB_Position_Lag1','BB_Position_Lag2']

def engineer_features_v3(df):
    c=df['Close'].squeeze(); h=df['High'].squeeze(); l=df['Low'].squeeze()
    o=df['Open'].squeeze(); v=df['Volume'].squeeze().astype(float)
    s50=SMAIndicator(c,50).sma_indicator(); s200=SMAIndicator(c,200).sma_indicator()
    e50=EMAIndicator(c,50).ema_indicator(); e200=EMAIndicator(c,200).ema_indicator()
    rsi=RSIIndicator(c,14).rsi(); bb=BollingerBands(c,20,2)
    bh,bl=bb.bollinger_hband(),bb.bollinger_lband()
    df['Dist_SMA50']=(c-s50)/s50; df['Dist_SMA200']=(c-s200)/s200
    df['Dist_EMA50']=(c-e50)/e50; df['Dist_EMA200']=(c-e200)/e200
    df['RSI']=rsi; df['BB_Position']=(c-bl)/(bh-bl); df['BB_Width']=(bh-bl)/c
    df['Return_1d']=c.pct_change(1); df['Return_5d']=c.pct_change(5); df['Return_20d']=c.pct_change(20)
    df['RVol_20d']=df['Return_1d'].rolling(20).std()
    df['SMA_Cross']=(s50>s200).astype(float); df['EMA_Cross']=(e50>e200).astype(float)
    vs20=v.rolling(20).mean(); df['Volume_Ratio_20d']=v/vs20
    obv=OnBalanceVolumeIndicator(c,v).on_balance_volume()
    obv_s=obv.rolling(10).apply(lambda x:np.polyfit(np.arange(len(x)),x,1)[0] if len(x)==10 else 0,raw=True)
    df['OBV_Slope_10d']=obv_s/(v.rolling(10).mean()+1e-10)
    df['Volume_Price_Div']=(np.sign(c.pct_change(5))!=np.sign(v.pct_change(5))).astype(float)
    df['Volume_Zscore_20d']=(v-vs20)/(v.rolling(20).std()+1e-10)
    df['ROC_3d']=ROCIndicator(c,3).roc(); df['ROC_10d']=ROCIndicator(c,10).roc()
    df['ROC_20d']=ROCIndicator(c,20).roc(); df['ROC_60d']=ROCIndicator(c,60).roc()
    atr=AverageTrueRange(h,l,c,14).average_true_range(); df['ATR_Norm']=atr/c
    lhl=np.log(h/l)**2; lco=np.log(c/o)**2
    df['GK_Vol_20d']=(0.5*lhl-(2*np.log(2)-1)*lco).rolling(20).mean()
    df['Intraday_Range']=(h-l)/c
    s20=SMAIndicator(c,20).sma_indicator()
    df['Zscore_20d']=(c-s20)/(c.rolling(20).std()+1e-10)
    df['Zscore_50d']=(c-s50)/(c.rolling(50).std()+1e-10)
    df['ADX_14']=ADXIndicator(h,l,c,14).adx()
    macd=MACD(c,26,12,9); df['MACD_Hist_Norm']=macd.macd_diff()/c
    df['Day_of_Week']=df.index.dayofweek.astype(float); df['Month']=df.index.month.astype(float)
    qe=pd.Series(df.index,index=df.index).apply(lambda d:1.0 if d.month in[3,6,9,12] and d.day>=25 else 0.0)
    df['Quarter_End']=qe.values
    df['RSI_Lag1']=rsi.shift(1); df['RSI_Lag2']=rsi.shift(2)
    df['Return_1d_Lag1']=df['Return_1d'].shift(1); df['Return_1d_Lag2']=df['Return_1d'].shift(2)
    df['BB_Position_Lag1']=df['BB_Position'].shift(1); df['BB_Position_Lag2']=df['BB_Position'].shift(2)
    df['Target_Return']=c.pct_change(1).shift(-1); df['Volatility_20d']=df['RVol_20d']
    return df

# =============================================================================
# V2 MODELS
# =============================================================================
def train_rf_v2(X,y):
    m=RandomForestRegressor(n_estimators=200,max_depth=6,min_samples_leaf=10,max_features='sqrt',random_state=42,n_jobs=-1)
    m.fit(X,y); return m
def train_xgb_v2(X,y):
    m=xgb.XGBRegressor(objective='reg:squarederror',n_estimators=200,max_depth=4,learning_rate=0.05,
                        subsample=0.8,colsample_bytree=0.8,reg_alpha=0.5,reg_lambda=2.0,random_state=42,n_jobs=-1)
    m.fit(X,y,verbose=False); return m
def predict_v2(models,X):
    return np.clip(0.8*models[0].predict(X)+0.2*models[1].predict(X),-0.08,0.08)

# =============================================================================
# V3 MODELS (Stacking Ensemble)
# =============================================================================
def train_lgbm_v3(Xt,yt,Xv,yv):
    m=lgb.LGBMRegressor(n_estimators=1000,max_depth=5,learning_rate=0.03,subsample=0.7,colsample_bytree=0.7,
                         reg_alpha=0.3,reg_lambda=1.5,min_child_samples=20,random_state=42,verbose=-1,n_jobs=-1)
    m.fit(Xt,yt,eval_set=[(Xv,yv)],callbacks=[lgb.early_stopping(50,verbose=False)]); return m

def train_xgb_v3(Xt,yt,Xv,yv):
    m=xgb.XGBRegressor(objective='reg:squarederror',n_estimators=1000,max_depth=4,learning_rate=0.03,
                        subsample=0.7,colsample_bytree=0.7,reg_alpha=0.5,reg_lambda=2.0,
                        random_state=42,n_jobs=-1,early_stopping_rounds=50)
    m.fit(Xt,yt,eval_set=[(Xv,yv)],verbose=False); return m

def train_rf_v3(X,y):
    m=RandomForestRegressor(n_estimators=300,max_depth=8,min_samples_leaf=15,max_features=0.5,random_state=42,n_jobs=-1)
    m.fit(X,y); return m

def build_stacking_ensemble(X_train,y_train,X_val,y_val):
    tscv=TimeSeriesSplit(n_splits=5); oof=np.zeros((len(X_train),3))
    for _,(ti,vi) in enumerate(tscv.split(X_train)):
        Xft,yft=X_train[ti],y_train[ti]; Xfv,yfv=X_train[vi],y_train[vi]
        oof[vi,0]=train_lgbm_v3(Xft,yft,Xfv,yfv).predict(Xfv)
        oof[vi,1]=train_xgb_v3(Xft,yft,Xfv,yfv).predict(Xfv)
        oof[vi,2]=train_rf_v3(Xft,yft).predict(Xfv)
    mask=np.any(oof!=0,axis=1); meta=Ridge(alpha=0.01); meta.fit(oof[mask],y_train[mask])
    fl=train_lgbm_v3(X_train,y_train,X_val,y_val)
    fx=train_xgb_v3(X_train,y_train,X_val,y_val)
    fr=train_rf_v3(X_train,y_train)
    return {'lgbm':fl,'xgb':fx,'rf':fr,'meta':meta}

def predict_v3(ens,X):
    st=np.column_stack([ens['lgbm'].predict(X),ens['xgb'].predict(X),ens['rf'].predict(X)])
    return np.clip(ens['meta'].predict(st),-0.08,0.08)

# =============================================================================
# PREDICT TICKER (used by dashboard + CLI)
# =============================================================================
def predict_ticker(symbol,cache_dir=None,verbose=True,version=None,**kwargs):
    cache_dir=cache_dir or CACHE_DIR; ver=version or MODEL_VERSION
    raw=fetch_stock_data([symbol],cache_dir=cache_dir)
    if symbol not in raw or raw[symbol].empty: return {"symbol":symbol,"error":"No data available"}

    df=raw[symbol].copy()
    if ver=='v3' and HAS_LGBM:
        df=engineer_features_v3(df); fcols=V3_FEATURE_COLS
    else:
        df=engineer_features_v2(df); fcols=V2_FEATURE_COLS; ver='v2'

    latest_row=df.iloc[-1:]; dc=df.dropna(subset=['Target_Return']+fcols).copy()
    if len(dc)<300: return {"symbol":symbol,"error":f"Insufficient data ({len(dc)} rows)"}

    aX=dc[fcols].values; ay=dc['Target_Return'].values; te=len(aX)
    vs=int(te*0.85); Xtr,ytr=aX[:vs],ay[:vs]; Xvl,yvl=aX[vs:],ay[vs:]

    scaler=StandardScaler(); scaler.fit(aX[:te])
    Xtr_s=scaler.transform(Xtr); Xvl_s=scaler.transform(Xvl)

    if ver=='v3':
        model=build_stacking_ensemble(Xtr_s,ytr,Xvl_s,yvl); pf=predict_v3
    else:
        rf=train_rf_v2(scaler.transform(aX[:te]),ay[:te])
        xm=train_xgb_v2(scaler.transform(aX[:te]),ay[:te])
        model=(rf,xm); pf=predict_v2

    # Rescaling factor
    rn=min(252,te); Xrc=scaler.transform(aX[te-rn:te])
    cp=pf(model,Xrc); ps=np.std(cp); acs=np.std(ay[te-rn:te])
    sf=acs/ps if ps>1e-10 else 1.0

    # Eval on validation
    yp=pf(model,Xvl_s)*sf
    mae_r=mean_absolute_error(yvl,yp); rmse_r=np.sqrt(mean_squared_error(yvl,yp))
    dir_acc=np.mean(np.sign(yp)==np.sign(yvl))*100

    # Quick backtest on validation
    btc,bts=10000.0,0.0; btp=[]
    for j in range(len(Xvl)):
        pr=yp[j]; bp=float(dc['Close'].iloc[vs+j]) if vs+j<len(dc) else float(dc['Close'].iloc[-1])
        if pr>=THRESHOLD and btc>0: bts=btc/bp; btc=0
        elif pr<=-THRESHOLD and bts>0: btc=bts*bp; bts=0
        btp.append(btc+bts*bp)
    bt_ret=(btp[-1]/10000-1)*100 if btp else 0
    tc=dc['Close'].values[vs:vs+len(btp)]
    bnh_ret=(tc[-1]/tc[0]-1)*100 if len(tc)>0 else 0
    bd=np.diff(btp)/np.array(btp[:-1]) if len(btp)>1 else [0]
    bt_sh=float(np.mean(bd)/np.std(bd)*np.sqrt(252)) if np.std(bd)>0 else 0
    bpk=np.maximum.accumulate(btp) if btp else [1]
    bt_dd=float(((np.array(btp)-bpk)/bpk).min())*100

    # Predict next day
    lf=latest_row[fcols].values
    if np.isnan(lf).any(): lf=aX[-1:].copy()
    ls=scaler.transform(lf.reshape(1,-1))
    rp=pf(model,ls)[0]; rsp=rp*sf
    cp_price=float(df['Close'].iloc[-1]); pp=cp_price*(1+rsp)

    # Per-model breakdown
    if ver=='v3':
        mpreds={"LGBM":round(cp_price*(1+float(model['lgbm'].predict(ls)[0])*sf),2),
                "XGB":round(cp_price*(1+float(model['xgb'].predict(ls)[0])*sf),2),
                "RF":round(cp_price*(1+float(model['rf'].predict(ls)[0])*sf),2)}
    else:
        mpreds={"RF":round(cp_price*(1+float(model[0].predict(ls)[0])*sf),2),
                "XGB":round(cp_price*(1+float(model[1].predict(ls)[0])*sf),2)}

    svr,mc,qr=_fetch_svr(symbol)
    pp_pct=rsp*100; sok=(svr is None)or(svr<=7); ste=(svr is not None)and(svr>=15)
    if pp_pct>=2.0 and sok: sig="BUY"
    elif pp_pct<=-2.0 or ste: sig="SELL"
    else: sig="HOLD"

    vl="V3 Stacking" if ver=='v3' else "V2 RF+XGB"
    result={"symbol":symbol,"current_price":round(cp_price,2),"predicted_price":round(float(pp),2),
            "pct_change":round(float(rsp)*100,2),"signal":sig,
            "signal_rules":"BUY: chg>=+2% & SVR<=7 | SELL: chg<=-2% or SVR>=15",
            "model_version":vl,"model_predictions":mpreds,"scale_factor":round(sf,1),
            "svr":svr,"market_cap":mc,"quarterly_revenue":qr,
            "backtest_mae_pct":round(float(mae_r)*100,3),"backtest_rmse_pct":round(float(rmse_r)*100,3),
            "direction_accuracy":round(float(dir_acc),1),
            "backtest_mae":round(float(mae_r)*cp_price,2),"backtest_rmse":round(float(rmse_r)*cp_price,2),
            "data_points":len(dc),"train_window":te,"train_window_setting":"All data",
            "weight_rf":0.8,"weight_xgb":0.2,"last_date":str(df.index[-1].date()),
            "backtest":{"strategy_return":round(bt_ret,2),"buyhold_return":round(bnh_ret,2),
                        "sharpe":round(bt_sh,2),"max_drawdown":round(bt_dd,1),"test_days":len(btp)}}
    if verbose:
        svr_s=f"{svr:.1f}x" if svr else "N/A"
        print(f"\n{'='*55}\n  {symbol}  ({vl}, scale {sf:.1f}x)\n{'='*55}")
        print(f"  Current:   ${cp_price:.2f}  |  Predicted: ${pp:.2f} ({pp_pct:+.2f}%)")
        print(f"  Signal:    {sig} (SVR {svr_s})  |  Dir Acc: {dir_acc:.1f}%")
        print(f"  Backtest:  Strategy {bt_ret:+.1f}% vs B&H {bnh_ret:+.1f}% | Sharpe {bt_sh:.2f}")
        print(f"  Models:    {mpreds}")
        print(f"  Data:      {len(dc)} rows through {df.index[-1].date()}")
    return result

# =============================================================================
# COMPARE BOTH MODELS (single ticker)
# =============================================================================
def predict_ticker_compare(symbol, cache_dir=None, verbose=False):
    """Run both V2 and V3 on a single ticker and return combined result."""
    cache_dir = cache_dir or CACHE_DIR
    r_v2 = predict_ticker(symbol, cache_dir=cache_dir, verbose=False, version='v2')
    r_v3 = predict_ticker(symbol, cache_dir=cache_dir, verbose=False, version='v3') if HAS_LGBM else None

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

    result = {
        'symbol': symbol,
        'current_price': r_v2.get('current_price') or (r_v3 or {}).get('current_price'),
        'last_date': r_v2.get('last_date') or (r_v3 or {}).get('last_date'),
        'svr': r_v2.get('svr'),
        'market_cap': r_v2.get('market_cap'),
        'quarterly_revenue': r_v2.get('quarterly_revenue'),
        'consensus_signal': consensus,
        'confidence': confidence,
        'v2': r_v2 if 'error' not in r_v2 else None,
        'v3': r_v3 if r_v3 and 'error' not in r_v3 else None,
    }
    if verbose:
        cp = result['current_price']
        p2 = r_v2.get('predicted_price', '?') if 'error' not in r_v2 else '?'
        p3 = (r_v3 or {}).get('predicted_price', '?') if r_v3 and 'error' not in r_v3 else '?'
        c2 = r_v2.get('pct_change', 0) if 'error' not in r_v2 else 0
        c3 = (r_v3 or {}).get('pct_change', 0) if r_v3 and 'error' not in r_v3 else 0
        print(f"\n{'='*60}\n  {symbol}  COMPARE\n{'='*60}")
        print(f"  Current: ${cp}  |  V2: ${p2} ({c2:+.2f}%)  |  V3: ${p3} ({c3:+.2f}%)")
        print(f"  V2 Signal: {sig_v2}  |  V3 Signal: {sig_v3}  |  Consensus: {consensus} ({confidence})")
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
                print(f"  [{i+1:3d}/{len(symbols)}] {s:<6} V2:{v2c:+6.2f}% V3:{v3c:+6.2f}% → {sig} ({conf})")
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
def backtest_symbol(symbol,cache_dir=None,version=None,initial_cash=10000):
    cache_dir=cache_dir or CACHE_DIR; ver=version or MODEL_VERSION
    raw=fetch_stock_data([symbol],cache_dir=cache_dir)
    if symbol not in raw or raw[symbol].empty: return None
    df=raw[symbol].copy()
    if ver=='v3' and HAS_LGBM:
        df=engineer_features_v3(df); fc=V3_FEATURE_COLS; rf_freq=RETRAIN_FREQ_V3
    else:
        df=engineer_features_v2(df); fc=V2_FEATURE_COLS; rf_freq=RETRAIN_FREQ_V2; ver='v2'
    dc=df.dropna(subset=['Target_Return']+fc).copy()
    bt_s='2015-01-02'; bm=dc.index>=pd.Timestamp(bt_s)
    if bm.sum()==0: print(f"No data after {bt_s}"); return None
    bsi=np.argmax(bm); aX=dc[fc].values; ay=dc['Target_Return'].values
    ap=dc['Close'].values.ravel()
    cash,sh=initial_cash,0.0; port,sigs=[],[]; mdl=None; sc=StandardScaler(); rc=0; sf=1.0
    for i in range(bsi,len(aX)-1):
        if mdl is None or rc>=rf_freq:
            vs=int(i*0.85); sc.fit(aX[:i])
            if ver=='v3':
                mdl=build_stacking_ensemble(sc.transform(aX[:vs]),ay[:vs],sc.transform(aX[vs:i]),ay[vs:i]); pf=predict_v3
            else:
                mdl=(train_rf_v2(sc.transform(aX[:i]),ay[:i]),train_xgb_v2(sc.transform(aX[:i]),ay[:i])); pf=predict_v2
            rn=min(252,i); cp2=pf(mdl,sc.transform(aX[i-rn:i])); ps2=np.std(cp2)
            sf=np.std(ay[i-rn:i])/ps2 if ps2>1e-10 else 1.0; rc=0
        p=pf(mdl,sc.transform(aX[i:i+1]))[0]*sf; pr=float(ap[i])
        if p>=THRESHOLD and cash>0: sh=cash/pr; cash=0; sigs.append('BUY')
        elif p<=-THRESHOLD and sh>0: cash=sh*pr; sh=0; sigs.append('SELL')
        else: sigs.append('HOLD')
        port.append(cash+sh*pr); rc+=1
    port=np.array(port); sr=(port[-1]/initial_cash-1)*100
    tp=ap[bsi:bsi+len(port)]; bnh=initial_cash*(tp/tp[0]); br=(bnh[-1]/initial_cash-1)*100
    dr=np.diff(port)/port[:-1]; sh_r=float(np.mean(dr)/np.std(dr)*np.sqrt(252)) if np.std(dr)>0 else 0
    pk=np.maximum.accumulate(port); mdd=float(((port-pk)/pk).min())*100
    nb,ns=sigs.count('BUY'),sigs.count('SELL')
    print(f"\n{'='*60}\n  BACKTEST: {symbol} ({ver.upper()})\n{'='*60}")
    print(f"  Period: {len(port)} days | Signals: {nb} BUY, {ns} SELL, {sigs.count('HOLD')} HOLD")
    print(f"  Strategy: {sr:+.1f}% | B&H: {br:+.1f}% | Sharpe: {sh_r:.2f} | MaxDD: {mdd:.1f}%")
    return port

# =============================================================================
# CLI
# =============================================================================
def main():
    p=argparse.ArgumentParser(description="Finance Model V4")
    p.add_argument('--ticker','-t',type=str); p.add_argument('--report','-r',action='store_true')
    p.add_argument('--backtest','-b',type=str); p.add_argument('--version','-v',type=str,choices=['v2','v3'])
    p.add_argument('--cache-dir',type=str,default=CACHE_DIR); p.add_argument('--top',type=int,default=10)
    a=p.parse_args()
    if a.ticker: predict_ticker(a.ticker.upper(),cache_dir=a.cache_dir,version=a.version)
    elif a.backtest: backtest_symbol(a.backtest.upper(),cache_dir=a.cache_dir,version=a.version)
    elif a.report: daily_scan(cache_dir=a.cache_dir,top_n=a.top)
    else: daily_scan(cache_dir=a.cache_dir,top_n=a.top)

if __name__=='__main__': main()
