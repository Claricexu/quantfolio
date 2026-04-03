"""
Quantfolio Backtest — Lite vs Pro vs Diverse Ensemble
======================================================
Tests whether adding a neural net (MLP) improves the stacking ensemble
by providing genuine model diversity (tree + tree + neural net).

Three models compared:
  Lite:    RF + XGB (fixed 80/20), 13 features
  Pro:     LightGBM + XGBoost + RF (inverse-MAE weighted), 22 features
  Diverse: LightGBM + RF + MLP (inverse-MAE weighted), 22 features

All use Z-score ±2.5σ signal strategy with 126-day rolling lookback.
All models tuned for efficiency (reduced estimators, 3-fold OOF).

Usage:
  python backtest_diverse_ensemble.py
"""
import sys
import time
import pandas as pd
import numpy as np
import warnings
import os

from ta.trend import SMAIndicator, EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator, ROCIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import OnBalanceVolumeIndicator

from sklearn.ensemble import RandomForestRegressor
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
import xgboost as xgb
import lightgbm as lgb
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

warnings.filterwarnings('ignore')
os.environ['PYTHONWARNINGS'] = 'ignore'
os.environ['LOKY_MAX_CPU_COUNT'] = '1'   # prevent joblib deadlock on Windows

# Force sklearn to use sequential backend (fixes Windows parallel hang)
try:
    from joblib import parallel_config
    parallel_config(backend='sequential')
except ImportError:
    try:
        import joblib
        joblib.Parallel(n_jobs=1, backend='sequential')
    except Exception:
        pass

# =============================================================================
# CONFIGURATION
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(SCRIPT_DIR, "data_cache", "SPY.csv")
BACKTEST_START = '2015-01-02'
RETRAIN_FREQ = 63
INITIAL_CASH = 10000
THRESHOLD = 2.5
ZSCORE_LOOKBACK = 126
OOF_FOLDS = 3           # reduced from 5 for speed
OUTPUT_CHART = os.path.join(SCRIPT_DIR, "diverse_ensemble_comparison.png")


# =============================================================================
# DATA
# =============================================================================
def load_data(path=DATA_PATH):
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.sort_index(inplace=True)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    df.dropna(subset=['Close'], inplace=True)
    return df


# =============================================================================
# FEATURES — Lite (13)
# =============================================================================
V2_FEATURE_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'Dist_EMA50', 'Dist_EMA200',
    'RSI', 'BB_Position', 'BB_Width',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d',
    'SMA_Cross', 'EMA_Cross',
]

def engineer_features_v2(df):
    c = df['Close'].squeeze()
    s50 = SMAIndicator(c, 50).sma_indicator()
    s200 = SMAIndicator(c, 200).sma_indicator()
    e50 = EMAIndicator(c, 50).ema_indicator()
    e200 = EMAIndicator(c, 200).ema_indicator()
    rsi = RSIIndicator(c, 14).rsi()
    bb = BollingerBands(c, 20, 2)
    bh, bl = bb.bollinger_hband(), bb.bollinger_lband()
    df['Dist_SMA50'] = (c - s50) / s50
    df['Dist_SMA200'] = (c - s200) / s200
    df['Dist_EMA50'] = (c - e50) / e50
    df['Dist_EMA200'] = (c - e200) / e200
    df['RSI'] = rsi
    df['BB_Position'] = (c - bl) / (bh - bl)
    df['BB_Width'] = (bh - bl) / c
    df['Return_1d'] = c.pct_change(1)
    df['Return_5d'] = c.pct_change(5)
    df['Return_20d'] = c.pct_change(20)
    df['RVol_20d'] = df['Return_1d'].rolling(20).std()
    df['SMA_Cross'] = (s50 > s200).astype(float)
    df['EMA_Cross'] = (e50 > e200).astype(float)
    df['Target_Return'] = c.pct_change(1).shift(-1)
    return df


# =============================================================================
# FEATURES — Pro / Diverse (22)
# =============================================================================
V3_FEATURE_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'RSI', 'BB_Position',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d', 'SMA_Cross',
    'Volume_Ratio_20d', 'OBV_Slope_10d', 'Volume_Zscore_20d',
    'ROC_10d', 'ROC_60d',
    'ATR_Norm', 'GK_Vol_20d',
    'Zscore_50d',
    'ADX_14', 'MACD_Hist_Norm',
    'RSI_Lag1', 'Return_1d_Lag1', 'BB_Position_Lag1',
]

def engineer_features_v3(df):
    c = df['Close'].squeeze(); h = df['High'].squeeze()
    l = df['Low'].squeeze(); o = df['Open'].squeeze()
    v = df['Volume'].squeeze().astype(float)
    s50 = SMAIndicator(c, 50).sma_indicator()
    s200 = SMAIndicator(c, 200).sma_indicator()
    rsi = RSIIndicator(c, 14).rsi()
    bb = BollingerBands(c, 20, 2)
    bh, bl = bb.bollinger_hband(), bb.bollinger_lband()
    df['Dist_SMA50'] = (c - s50) / s50
    df['Dist_SMA200'] = (c - s200) / s200
    df['RSI'] = rsi
    df['BB_Position'] = (c - bl) / (bh - bl)
    df['Return_1d'] = c.pct_change(1)
    df['Return_5d'] = c.pct_change(5)
    df['Return_20d'] = c.pct_change(20)
    df['RVol_20d'] = df['Return_1d'].rolling(20).std()
    df['SMA_Cross'] = (s50 > s200).astype(float)
    vs20 = v.rolling(20).mean()
    df['Volume_Ratio_20d'] = v / vs20
    obv = OnBalanceVolumeIndicator(c, v).on_balance_volume()
    obv_s = obv.rolling(10).apply(
        lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 10 else 0, raw=True)
    df['OBV_Slope_10d'] = obv_s / (v.rolling(10).mean() + 1e-10)
    df['Volume_Zscore_20d'] = (v - vs20) / (v.rolling(20).std() + 1e-10)
    df['ROC_10d'] = ROCIndicator(c, 10).roc()
    df['ROC_60d'] = ROCIndicator(c, 60).roc()
    atr = AverageTrueRange(h, l, c, 14).average_true_range()
    df['ATR_Norm'] = atr / c
    lhl = np.log(h / l) ** 2; lco = np.log(c / o) ** 2
    df['GK_Vol_20d'] = (0.5 * lhl - (2 * np.log(2) - 1) * lco).rolling(20).mean()
    df['Zscore_50d'] = (c - s50) / (c.rolling(50).std() + 1e-10)
    df['ADX_14'] = ADXIndicator(h, l, c, 14).adx()
    macd = MACD(c, 26, 12, 9)
    df['MACD_Hist_Norm'] = macd.macd_diff() / c
    df['RSI_Lag1'] = rsi.shift(1)
    df['Return_1d_Lag1'] = df['Return_1d'].shift(1)
    df['BB_Position_Lag1'] = df['BB_Position'].shift(1)
    df['Target_Return'] = c.pct_change(1).shift(-1)
    return df


# =============================================================================
# MODEL BUILDERS (tuned for efficiency)
# =============================================================================

# --- Safe RF predict (bypasses joblib Parallel entirely) ---
def rf_predict(rf_model, X):
    """Manual RF predict: average individual tree predictions.
    Avoids sklearn's joblib Parallel which deadlocks on Windows."""
    return np.mean([tree.predict(X) for tree in rf_model.estimators_], axis=0)

# --- Lite components ---
def train_rf_lite(X, y):
    m = RandomForestRegressor(n_estimators=100, max_depth=6, min_samples_leaf=10,
                              max_features='sqrt', random_state=42, n_jobs=1)
    m.fit(X, y); return m

def train_xgb_lite(X, y):
    m = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=100, max_depth=4,
                          learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                          reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1)
    m.fit(X, y, verbose=False); return m

def predict_lite(models, X):
    return np.clip(0.8 * rf_predict(models[0], X) + 0.2 * models[1].predict(X), -0.08, 0.08)


# --- Shared tree learners (efficient) ---
def train_lgbm(X_tr, y_tr, X_val, y_val):
    m = lgb.LGBMRegressor(n_estimators=500, max_depth=5, learning_rate=0.05,
                           subsample=0.7, colsample_bytree=0.7, reg_alpha=0.3, reg_lambda=1.5,
                           min_child_samples=20, random_state=42, verbose=-1, n_jobs=1)
    m.fit(X_tr, y_tr, eval_set=[(X_val, y_val)],
          callbacks=[lgb.early_stopping(30, verbose=False)])
    return m

def train_xgb_pro(X_tr, y_tr, X_val, y_val):
    m = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=500, max_depth=4,
                          learning_rate=0.05, subsample=0.7, colsample_bytree=0.7,
                          reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1,
                          early_stopping_rounds=30)
    m.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
    return m

def train_rf_pro(X, y):
    m = RandomForestRegressor(n_estimators=150, max_depth=8, min_samples_leaf=15,
                              max_features=0.5, random_state=42, n_jobs=1)
    m.fit(X, y); return m


# --- MLP (neural net) ---
def train_mlp(X, y):
    m = MLPRegressor(hidden_layer_sizes=(64, 32), activation='relu', solver='adam',
                     alpha=0.01, learning_rate='adaptive', learning_rate_init=0.001,
                     max_iter=500, early_stopping=True, validation_fraction=0.15,
                     n_iter_no_change=20, random_state=42)
    m.fit(X, y); return m


# =============================================================================
# ENSEMBLE BUILDERS
# =============================================================================

def build_pro_ensemble(X_train, y_train, X_val, y_val):
    """Pro: LightGBM + XGBoost + RF"""
    tscv = TimeSeriesSplit(n_splits=OOF_FOLDS)
    oof = np.zeros((len(X_train), 3))
    for _, (ti, vi) in enumerate(tscv.split(X_train)):
        Xft, yft = X_train[ti], y_train[ti]
        Xfv, yfv = X_train[vi], y_train[vi]
        oof[vi, 0] = train_lgbm(Xft, yft, Xfv, yfv).predict(Xfv)
        oof[vi, 1] = train_xgb_pro(Xft, yft, Xfv, yfv).predict(Xfv)
        oof[vi, 2] = rf_predict(train_rf_pro(Xft, yft), Xfv)
    mask = np.any(oof != 0, axis=1)
    mae = np.array([np.mean(np.abs(oof[mask, j] - y_train[mask])) for j in range(3)])
    inv = 1.0 / (mae + 1e-10); w = inv / inv.sum()
    fl = train_lgbm(X_train, y_train, X_val, y_val)
    fx = train_xgb_pro(X_train, y_train, X_val, y_val)
    fr = train_rf_pro(X_train, y_train)
    return {'lgbm': fl, 'xgb': fx, 'rf': fr, 'weights': w}

def predict_pro(ens, X):
    w = ens['weights']
    return w[0]*ens['lgbm'].predict(X) + w[1]*ens['xgb'].predict(X) + w[2]*rf_predict(ens['rf'], X)


def build_diverse_ensemble(X_train, y_train, X_val, y_val):
    """Diverse: LightGBM + RF + MLP (neural net)"""
    tscv = TimeSeriesSplit(n_splits=OOF_FOLDS)
    oof = np.zeros((len(X_train), 3))
    for _, (ti, vi) in enumerate(tscv.split(X_train)):
        Xft, yft = X_train[ti], y_train[ti]
        Xfv, yfv = X_train[vi], y_train[vi]
        oof[vi, 0] = train_lgbm(Xft, yft, Xfv, yfv).predict(Xfv)
        oof[vi, 1] = rf_predict(train_rf_pro(Xft, yft), Xfv)
        oof[vi, 2] = train_mlp(Xft, yft).predict(Xfv)
    mask = np.any(oof != 0, axis=1)
    mae = np.array([np.mean(np.abs(oof[mask, j] - y_train[mask])) for j in range(3)])
    inv = 1.0 / (mae + 1e-10); w = inv / inv.sum()
    fl = train_lgbm(X_train, y_train, X_val, y_val)
    fr = train_rf_pro(X_train, y_train)
    fm = train_mlp(X_train, y_train)
    return {'lgbm': fl, 'rf': fr, 'mlp': fm, 'weights': w}

def predict_diverse(ens, X):
    w = ens['weights']
    return w[0]*ens['lgbm'].predict(X) + w[1]*rf_predict(ens['rf'], X) + w[2]*ens['mlp'].predict(X)


# =============================================================================
# WALK-FORWARD BACKTEST — Z-SCORE SIGNAL
# =============================================================================
def walk_forward(df, feature_cols, model_name, build_fn, predict_fn,
                 backtest_start, initial_cash=10000, threshold=2.5):
    df_clean = df.dropna(subset=['Target_Return'] + feature_cols).copy()
    bt_mask = df_clean.index >= pd.Timestamp(backtest_start)
    if bt_mask.sum() == 0:
        raise ValueError(f"No data after {backtest_start}")
    bt_start_idx = np.argmax(bt_mask)
    all_X = df_clean[feature_cols].values
    all_y = df_clean['Target_Return'].values
    all_prices = df_clean['Close'].values.ravel()
    all_dates = df_clean.index

    cash, shares = initial_cash, 0.0
    portfolio, signals = [], []
    model = None; scaler = StandardScaler()
    retrain_counter = 0; pred_history = []
    retrain_times = []

    total_test = len(all_X) - bt_start_idx - 1
    n_retrains = total_test // RETRAIN_FREQ + 1
    print(f"  [{model_name}] {total_test} days, ~{n_retrains} retrains, Z ±{threshold}σ")

    for i in range(bt_start_idx, len(all_X) - 1):
        if model is None or retrain_counter >= RETRAIN_FREQ:
            t0 = time.time()
            train_end = i
            X_all = all_X[:train_end]; y_all = all_y[:train_end]
            vs = int(len(X_all) * 0.85)
            scaler.fit(X_all)
            X_tr_s = scaler.transform(X_all[:vs])
            X_val_s = scaler.transform(X_all[vs:])
            y_tr, y_val = y_all[:vs], y_all[vs:]

            model = build_fn(X_tr_s, y_tr, X_val_s, y_val)
            elapsed = time.time() - t0
            retrain_times.append(elapsed)
            done = len(retrain_times)
            pct = done * 100 // n_retrains
            print(f"    retrain {done}/{n_retrains} ({pct}%) — {elapsed:.1f}s", flush=True)

            if not pred_history:
                seed = predict_fn(model, X_val_s)
                pred_history = list(seed[-ZSCORE_LOOKBACK:])
            retrain_counter = 0

        X_today = scaler.transform(all_X[i:i+1])
        raw_pred = predict_fn(model, X_today)[0]
        pred_history.append(raw_pred)
        if len(pred_history) > ZSCORE_LOOKBACK:
            pred_history = pred_history[-ZSCORE_LOOKBACK:]

        if len(pred_history) >= 20:
            hist = np.array(pred_history[:-1])
            mu, sigma = np.mean(hist), np.std(hist)
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0

        price = float(all_prices[i])
        if z >= threshold and cash > 0:
            shares = cash / price; cash = 0; signals.append('BUY')
        elif z <= -threshold and shares > 0:
            cash = shares * price; shares = 0; signals.append('SELL')
        else:
            signals.append('HOLD')

        portfolio.append(cash + shares * price)
        retrain_counter += 1

    n_buy, n_sell = signals.count('BUY'), signals.count('SELL')
    avg_train = np.mean(retrain_times) if retrain_times else 0
    print(f"  [{model_name}] {n_buy} BUY, {n_sell} SELL, {n_buy+n_sell} trades | "
          f"avg retrain: {avg_train:.1f}s × {len(retrain_times)} = {sum(retrain_times):.0f}s total")

    return {
        'dates': all_dates[bt_start_idx:bt_start_idx+len(portfolio)],
        'portfolio': np.array(portfolio),
        'signals': signals,
        'n_buy': n_buy, 'n_sell': n_sell,
        'avg_train_time': round(avg_train, 1),
        'total_train_time': round(sum(retrain_times), 0),
    }


def walk_forward_lite(df, backtest_start, initial_cash=10000, threshold=2.5):
    """Lite has different build interface (no val split for training)."""
    feature_cols = V2_FEATURE_COLS
    df_clean = df.dropna(subset=['Target_Return'] + feature_cols).copy()
    bt_mask = df_clean.index >= pd.Timestamp(backtest_start)
    if bt_mask.sum() == 0:
        raise ValueError(f"No data after {backtest_start}")
    bt_start_idx = np.argmax(bt_mask)
    all_X = df_clean[feature_cols].values
    all_y = df_clean['Target_Return'].values
    all_prices = df_clean['Close'].values.ravel()
    all_dates = df_clean.index

    cash, shares = initial_cash, 0.0
    portfolio, signals = [], []
    model = None; scaler = StandardScaler()
    retrain_counter = 0; pred_history = []
    retrain_times = []

    total_test = len(all_X) - bt_start_idx - 1
    n_retrains = total_test // RETRAIN_FREQ + 1
    print(f"  [Lite] {total_test} days, ~{n_retrains} retrains, Z ±{threshold}σ")

    for i in range(bt_start_idx, len(all_X) - 1):
        if model is None or retrain_counter >= RETRAIN_FREQ:
            t0 = time.time()
            train_end = i
            scaler.fit(all_X[:train_end])
            X_s = scaler.transform(all_X[:train_end])
            y_s = all_y[:train_end]
            model = (train_rf_lite(X_s, y_s), train_xgb_lite(X_s, y_s))
            elapsed = time.time() - t0
            retrain_times.append(elapsed)
            done = len(retrain_times)
            print(f"    retrain {done}/{n_retrains} ({done*100//n_retrains}%) — {elapsed:.1f}s", flush=True)

            if not pred_history:
                vs = int(train_end * 0.85)
                seed = predict_lite(model, scaler.transform(all_X[vs:train_end]))
                pred_history = list(seed[-ZSCORE_LOOKBACK:])
            retrain_counter = 0

        X_today = scaler.transform(all_X[i:i+1])
        raw_pred = predict_lite(model, X_today)[0]
        pred_history.append(raw_pred)
        if len(pred_history) > ZSCORE_LOOKBACK:
            pred_history = pred_history[-ZSCORE_LOOKBACK:]

        if len(pred_history) >= 20:
            hist = np.array(pred_history[:-1])
            mu, sigma = np.mean(hist), np.std(hist)
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0

        price = float(all_prices[i])
        if z >= threshold and cash > 0:
            shares = cash / price; cash = 0; signals.append('BUY')
        elif z <= -threshold and shares > 0:
            cash = shares * price; shares = 0; signals.append('SELL')
        else:
            signals.append('HOLD')

        portfolio.append(cash + shares * price)
        retrain_counter += 1

    n_buy, n_sell = signals.count('BUY'), signals.count('SELL')
    avg_train = np.mean(retrain_times) if retrain_times else 0
    print(f"  [Lite] {n_buy} BUY, {n_sell} SELL, {n_buy+n_sell} trades | "
          f"avg retrain: {avg_train:.1f}s × {len(retrain_times)} = {sum(retrain_times):.0f}s total")

    return {
        'dates': all_dates[bt_start_idx:bt_start_idx+len(portfolio)],
        'portfolio': np.array(portfolio),
        'signals': signals,
        'n_buy': n_buy, 'n_sell': n_sell,
        'avg_train_time': round(avg_train, 1),
        'total_train_time': round(sum(retrain_times), 0),
    }


# =============================================================================
# METRICS
# =============================================================================
def compute_metrics(portfolio, initial_cash):
    total_ret = (portfolio[-1] / initial_cash - 1) * 100
    n_years = len(portfolio) / 252
    annual_ret = ((portfolio[-1] / initial_cash) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0
    daily_rets = np.diff(portfolio) / portfolio[:-1]
    sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252)) if np.std(daily_rets) > 0 else 0
    peak = np.maximum.accumulate(portfolio)
    max_dd = float(((portfolio - peak) / peak).min()) * 100
    return {
        'final_value': round(portfolio[-1], 2),
        'total_return': round(total_ret, 1),
        'annual_return': round(annual_ret, 1),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 1),
    }

def bootstrap_sharpe(portfolio, n_bootstrap=10000, ci_level=0.95, seed=42):
    daily_rets = np.diff(portfolio) / portfolio[:-1]
    n = len(daily_rets)
    if n < 30 or np.std(daily_rets) == 0:
        return 0.0, 0.0, 0.0
    observed = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252))
    rng = np.random.RandomState(seed)
    boots = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        sample = rng.choice(daily_rets, size=n, replace=True)
        s = np.std(sample)
        boots[b] = np.mean(sample) / s * np.sqrt(252) if s > 0 else 0.0
    alpha = (1 - ci_level) / 2
    return observed, np.percentile(boots, alpha*100), np.percentile(boots, (1-alpha)*100)


# =============================================================================
# CHART
# =============================================================================
def plot_comparison(dates, lite_eq, pro_eq, div_eq, bnh_eq, output_path, div_signals=None):
    fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True,
                              gridspec_kw={'height_ratios': [3, 1, 1]})
    ax1, ax2, ax3 = axes
    dp = pd.to_datetime(dates)

    ax1.plot(dp, lite_eq, label='Lite (RF+XGB)', lw=1.3, color='#2196F3')
    ax1.plot(dp, pro_eq, label='Pro (LGB+XGB+RF)', lw=1.3, color='#FF5722')
    ax1.plot(dp, div_eq, label='Diverse (LGB+RF+MLP)', lw=1.8, color='#4CAF50')
    ax1.plot(dp, bnh_eq, label='Buy & Hold', lw=1.0, color='#9E9E9E', ls='--')

    if div_signals:
        for i, sig in enumerate(div_signals):
            if sig == 'BUY':
                ax1.axvline(dp[i], alpha=0.15, color='green', lw=0.5)
            elif sig == 'SELL':
                ax1.axvline(dp[i], alpha=0.15, color='red', lw=0.5)

    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title('SPY Backtest: Lite vs Pro vs Diverse (MLP) vs Buy & Hold')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    for arr, label, color in [
        (lite_eq, 'Lite', '#2196F3'), (pro_eq, 'Pro', '#FF5722'),
        (div_eq, 'Diverse', '#4CAF50'), (bnh_eq, 'B&H', '#9E9E9E')]:
        peak = np.maximum.accumulate(arr)
        dd = (arr - peak) / peak * 100
        ax2.fill_between(dp, dd, 0, alpha=0.3, color=color, label=label)
        ax2.plot(dp, dd, lw=0.8, color=color)
    ax2.set_ylabel('Drawdown (%)'); ax2.legend(loc='lower left', fontsize=8); ax2.grid(True, alpha=0.3)

    if div_signals:
        pos, p = [], 0
        for sig in div_signals:
            if sig == 'BUY': p = 1
            elif sig == 'SELL': p = 0
            pos.append(p)
        ax3.fill_between(dp, pos, 0, alpha=0.4, color='#4CAF50', label='Diverse Invested')
        ax3.set_ylabel('Diverse Position'); ax3.set_yticks([0,1])
        ax3.set_yticklabels(['Cash','Invested']); ax3.legend(loc='upper left', fontsize=8)
        ax3.grid(True, alpha=0.3)

    ax3.set_xlabel('Date')
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\n  Chart saved: {output_path}")


# =============================================================================
# MAIN
# =============================================================================
def run():
    t_start = time.time()
    print("=" * 75)
    print("  QUANTFOLIO — DIVERSE ENSEMBLE BACKTEST (with MLP Neural Net)")
    print("=" * 75)
    print(f"  Lite:    RF (80%) + XGB (20%)               | 13 features")
    print(f"  Pro:     LightGBM + XGBoost + RF             | 22 features")
    print(f"  Diverse: LightGBM + RF + MLP neural net      | 22 features")
    print(f"  Signal:  Z-score ±{THRESHOLD}σ | Retrain every {RETRAIN_FREQ}d | {OOF_FOLDS}-fold OOF")
    print(f"  Capital: ${INITIAL_CASH:,}")

    print(f"\n[1/6] Loading SPY data...")
    df_raw = load_data()
    print(f"  {len(df_raw)} rows: {df_raw.index[0].date()} to {df_raw.index[-1].date()}")

    print(f"\n[2/6] Engineering features...")
    df_v2 = engineer_features_v2(df_raw.copy())
    df_v3 = engineer_features_v3(df_raw.copy())

    print(f"\n[3/6] Lite backtest...")
    res_lite = walk_forward_lite(df_v2, BACKTEST_START, INITIAL_CASH, THRESHOLD)

    print(f"\n[4/6] Pro backtest...")
    res_pro = walk_forward(df_v3, V3_FEATURE_COLS, 'Pro',
                           build_pro_ensemble, predict_pro,
                           BACKTEST_START, INITIAL_CASH, THRESHOLD)

    print(f"\n[5/6] Diverse backtest...")
    res_div = walk_forward(df_v3, V3_FEATURE_COLS, 'Diverse',
                           build_diverse_ensemble, predict_diverse,
                           BACKTEST_START, INITIAL_CASH, THRESHOLD)

    # Buy & Hold
    df_bnh = df_v2.dropna(subset=['Target_Return'] + V2_FEATURE_COLS).copy()
    bt_prices = df_bnh.loc[df_bnh.index >= pd.Timestamp(BACKTEST_START), 'Close'].values.ravel()
    bnh_eq = INITIAL_CASH * (bt_prices / bt_prices[0])

    print(f"\n[6/6] Metrics & bootstrap CIs...")
    m_l = compute_metrics(res_lite['portfolio'], INITIAL_CASH)
    m_p = compute_metrics(res_pro['portfolio'], INITIAL_CASH)
    m_d = compute_metrics(res_div['portfolio'], INITIAL_CASH)
    m_b = compute_metrics(bnh_eq, INITIAL_CASH)

    sh_l, sh_l_lo, sh_l_hi = bootstrap_sharpe(res_lite['portfolio'])
    sh_p, sh_p_lo, sh_p_hi = bootstrap_sharpe(res_pro['portfolio'])
    sh_d, sh_d_lo, sh_d_hi = bootstrap_sharpe(res_div['portfolio'])
    sh_b, sh_b_lo, sh_b_hi = bootstrap_sharpe(bnh_eq)

    total_time = time.time() - t_start

    # Print table
    print(f"\n{'='*90}")
    print(f"  SPY BACKTEST: {BACKTEST_START} -> {res_lite['dates'][-1].date()}")
    print(f"  Lite = RF+XGB  |  Pro = LGB+XGB+RF  |  Diverse = LGB+RF+MLP")
    print(f"{'='*90}\n")
    print(f"  {'':20s} {'Lite':>12s}  {'Pro':>12s}  {'Diverse':>12s}  {'Buy & Hold':>12s}")
    print(f"  {'─'*72}")
    print(f"  {'Final Value':20s} {'$'+str(m_l['final_value']):>12s}  {'$'+str(m_p['final_value']):>12s}  {'$'+str(m_d['final_value']):>12s}  {'$'+str(m_b['final_value']):>12s}")
    print(f"  {'Total Return':20s} {m_l['total_return']:>+11.1f}%  {m_p['total_return']:>+11.1f}%  {m_d['total_return']:>+11.1f}%  {m_b['total_return']:>+11.1f}%")
    print(f"  {'Annual Return':20s} {m_l['annual_return']:>+11.1f}%  {m_p['annual_return']:>+11.1f}%  {m_d['annual_return']:>+11.1f}%  {m_b['annual_return']:>+11.1f}%")
    print(f"  {'Sharpe Ratio':20s} {m_l['sharpe']:>12.2f}  {m_p['sharpe']:>12.2f}  {m_d['sharpe']:>12.2f}  {m_b['sharpe']:>12.2f}")
    print(f"  {'  95% CI':20s} [{sh_l_lo:+.2f},{sh_l_hi:+.2f}]  [{sh_p_lo:+.2f},{sh_p_hi:+.2f}]  [{sh_d_lo:+.2f},{sh_d_hi:+.2f}]  [{sh_b_lo:+.2f},{sh_b_hi:+.2f}]")
    print(f"  {'Max Drawdown':20s} {m_l['max_drawdown']:>11.1f}%  {m_p['max_drawdown']:>11.1f}%  {m_d['max_drawdown']:>11.1f}%  {m_b['max_drawdown']:>11.1f}%")
    print(f"  {'─'*72}")
    print(f"  {'BUY Signals':20s} {res_lite['n_buy']:>12d}  {res_pro['n_buy']:>12d}  {res_div['n_buy']:>12d}  {'--':>12s}")
    print(f"  {'SELL Signals':20s} {res_lite['n_sell']:>12d}  {res_pro['n_sell']:>12d}  {res_div['n_sell']:>12d}  {'--':>12s}")
    print(f"  {'Total Trades':20s} {res_lite['n_buy']+res_lite['n_sell']:>12d}  {res_pro['n_buy']+res_pro['n_sell']:>12d}  {res_div['n_buy']+res_div['n_sell']:>12d}  {'0':>12s}")
    print(f"  {'─'*72}")
    print(f"  {'Avg Retrain (s)':20s} {res_lite['avg_train_time']:>12.1f}  {res_pro['avg_train_time']:>12.1f}  {res_div['avg_train_time']:>12.1f}  {'--':>12s}")
    print(f"  {'Total Train (s)':20s} {res_lite['total_train_time']:>12.0f}  {res_pro['total_train_time']:>12.0f}  {res_div['total_train_time']:>12.0f}  {'--':>12s}")
    print(f"  {'─'*72}")
    print(f"\n  Total wall time: {total_time:.0f}s ({total_time/60:.1f}min)")

    # Chart
    min_len = min(len(res_lite['portfolio']), len(res_pro['portfolio']),
                  len(res_div['portfolio']), len(bnh_eq))
    plot_comparison(res_lite['dates'][:min_len],
                    res_lite['portfolio'][:min_len],
                    res_pro['portfolio'][:min_len],
                    res_div['portfolio'][:min_len],
                    bnh_eq[:min_len],
                    OUTPUT_CHART,
                    div_signals=res_div['signals'][:min_len])

    print(f"\n{'='*75}")
    print(f"  DONE")
    print(f"{'='*75}")


if __name__ == '__main__':
    run()
    sys.stdout.flush()
