"""
Finance Model V3 Advanced — Stacking Ensemble with Walk-Forward Backtest
=========================================================================
Compares against V2 (RF+XGB) with an advanced stacking ensemble:
  - 37 features (vs 13 in V2)
  - LightGBM + XGBoost + Random Forest base models
  - Ridge meta-learner on TimeSeriesSplit out-of-fold predictions
  - Dynamic volatility-adjusted trading thresholds
  - Proper walk-forward expanding-window backtest

Usage:
  python finance_model_v3_advanced.py
"""

import pandas as pd
import numpy as np
import warnings
import os
import sys
from datetime import datetime

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
import lightgbm as lgb
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

warnings.filterwarnings('ignore')
os.environ['PYTHONWARNINGS'] = 'ignore'

# =============================================================================
# CONFIGURATION
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(SCRIPT_DIR, "data_cache", "SPY.csv")
BACKTEST_START = '2015-01-02'
RETRAIN_FREQ_V2 = 20
RETRAIN_FREQ_V3 = 63
INITIAL_CASH = 10000
OUTPUT_CHART = os.path.join(SCRIPT_DIR, "v3_backtest_comparison.png")


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(path=DATA_PATH):
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.sort_index(inplace=True)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    df.dropna(subset=['Close'], inplace=True)
    return df


# =============================================================================
# V2 FEATURE ENGINEERING (exact replica of finance_model_v2.py)
# =============================================================================

V2_FEATURE_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'Dist_EMA50', 'Dist_EMA200',
    'RSI', 'BB_Position', 'BB_Width',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d',
    'SMA_Cross', 'EMA_Cross',
]

def engineer_features_v2(df):
    close = df['Close'].squeeze()
    sma50  = SMAIndicator(close, window=50).sma_indicator()
    sma200 = SMAIndicator(close, window=200).sma_indicator()
    ema50  = EMAIndicator(close, window=50).ema_indicator()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    rsi    = RSIIndicator(close, window=14).rsi()
    bb     = BollingerBands(close, window=20, window_dev=2)
    bb_high = bb.bollinger_hband()
    bb_low  = bb.bollinger_lband()

    df['Dist_SMA50']  = (close - sma50) / sma50
    df['Dist_SMA200'] = (close - sma200) / sma200
    df['Dist_EMA50']  = (close - ema50) / ema50
    df['Dist_EMA200'] = (close - ema200) / ema200
    df['RSI']         = rsi
    df['BB_Position'] = (close - bb_low) / (bb_high - bb_low)
    df['BB_Width']    = (bb_high - bb_low) / close
    df['Return_1d']   = close.pct_change(1)
    df['Return_5d']   = close.pct_change(5)
    df['Return_20d']  = close.pct_change(20)
    df['RVol_20d']    = df['Return_1d'].rolling(20).std()
    df['SMA_Cross']   = (sma50 > sma200).astype(float)
    df['EMA_Cross']   = (ema50 > ema200).astype(float)
    df['Target_Return'] = close.pct_change(1).shift(-1)
    df['Volatility_20d'] = df['RVol_20d']
    return df


# =============================================================================
# V3 FEATURE ENGINEERING (37 features)
# =============================================================================

V3_FEATURE_COLS = [
    # V2 core (13)
    'Dist_SMA50', 'Dist_SMA200', 'Dist_EMA50', 'Dist_EMA200',
    'RSI', 'BB_Position', 'BB_Width',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d',
    'SMA_Cross', 'EMA_Cross',
    # Volume (4)
    'Volume_Ratio_20d', 'OBV_Slope_10d', 'Volume_Price_Div', 'Volume_Zscore_20d',
    # Multi-timeframe momentum (4)
    'ROC_3d', 'ROC_10d', 'ROC_20d', 'ROC_60d',
    # Volatility (3)
    'ATR_Norm', 'GK_Vol_20d', 'Intraday_Range',
    # Mean reversion (2)
    'Zscore_20d', 'Zscore_50d',
    # Trend strength (2)
    'ADX_14', 'MACD_Hist_Norm',
    # Calendar (3)
    'Day_of_Week', 'Month', 'Quarter_End',
    # Lagged (6)
    'RSI_Lag1', 'RSI_Lag2', 'Return_1d_Lag1', 'Return_1d_Lag2',
    'BB_Position_Lag1', 'BB_Position_Lag2',
]

def engineer_features_v3(df):
    close = df['Close'].squeeze()
    high = df['High'].squeeze()
    low = df['Low'].squeeze()
    opn = df['Open'].squeeze()
    volume = df['Volume'].squeeze().astype(float)

    # --- V2 core indicators ---
    sma50  = SMAIndicator(close, window=50).sma_indicator()
    sma200 = SMAIndicator(close, window=200).sma_indicator()
    ema50  = EMAIndicator(close, window=50).ema_indicator()
    ema200 = EMAIndicator(close, window=200).ema_indicator()
    rsi    = RSIIndicator(close, window=14).rsi()
    bb     = BollingerBands(close, window=20, window_dev=2)
    bb_high = bb.bollinger_hband()
    bb_low  = bb.bollinger_lband()

    df['Dist_SMA50']  = (close - sma50) / sma50
    df['Dist_SMA200'] = (close - sma200) / sma200
    df['Dist_EMA50']  = (close - ema50) / ema50
    df['Dist_EMA200'] = (close - ema200) / ema200
    df['RSI']         = rsi
    df['BB_Position'] = (close - bb_low) / (bb_high - bb_low)
    df['BB_Width']    = (bb_high - bb_low) / close
    df['Return_1d']   = close.pct_change(1)
    df['Return_5d']   = close.pct_change(5)
    df['Return_20d']  = close.pct_change(20)
    df['RVol_20d']    = df['Return_1d'].rolling(20).std()
    df['SMA_Cross']   = (sma50 > sma200).astype(float)
    df['EMA_Cross']   = (ema50 > ema200).astype(float)

    # --- Volume features ---
    vol_sma20 = volume.rolling(20).mean()
    df['Volume_Ratio_20d'] = volume / vol_sma20

    obv = OnBalanceVolumeIndicator(close, volume).on_balance_volume()
    # OBV slope: linear regression slope over 10 days, normalized
    obv_series = obv.rolling(10).apply(
        lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 10 else 0,
        raw=True
    )
    df['OBV_Slope_10d'] = obv_series / (volume.rolling(10).mean() + 1e-10)

    price_dir = np.sign(close.pct_change(5))
    vol_dir = np.sign(volume.pct_change(5))
    df['Volume_Price_Div'] = (price_dir != vol_dir).astype(float)

    vol_std20 = volume.rolling(20).std()
    df['Volume_Zscore_20d'] = (volume - vol_sma20) / (vol_std20 + 1e-10)

    # --- Multi-timeframe momentum ---
    df['ROC_3d']  = ROCIndicator(close, window=3).roc()
    df['ROC_10d'] = ROCIndicator(close, window=10).roc()
    df['ROC_20d'] = ROCIndicator(close, window=20).roc()
    df['ROC_60d'] = ROCIndicator(close, window=60).roc()

    # --- Volatility features ---
    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    df['ATR_Norm'] = atr / close

    # Garman-Klass volatility
    log_hl = np.log(high / low) ** 2
    log_co = np.log(close / opn) ** 2
    gk = 0.5 * log_hl - (2 * np.log(2) - 1) * log_co
    df['GK_Vol_20d'] = gk.rolling(20).mean()

    df['Intraday_Range'] = (high - low) / close

    # --- Mean reversion ---
    sma20 = SMAIndicator(close, window=20).sma_indicator()
    std20 = close.rolling(20).std()
    std50 = close.rolling(50).std()
    df['Zscore_20d'] = (close - sma20) / (std20 + 1e-10)
    df['Zscore_50d'] = (close - sma50) / (std50 + 1e-10)

    # --- Trend strength ---
    df['ADX_14'] = ADXIndicator(high, low, close, window=14).adx()
    macd = MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df['MACD_Hist_Norm'] = macd.macd_diff() / close

    # --- Calendar features ---
    df['Day_of_Week'] = df.index.dayofweek.astype(float)
    df['Month'] = df.index.month.astype(float)
    # Quarter end: last 5 trading days of each quarter
    qe = pd.Series(df.index, index=df.index).apply(
        lambda d: 1.0 if d.month in [3, 6, 9, 12] and d.day >= 25 else 0.0
    )
    df['Quarter_End'] = qe.values

    # --- Lagged features ---
    df['RSI_Lag1'] = rsi.shift(1)
    df['RSI_Lag2'] = rsi.shift(2)
    df['Return_1d_Lag1'] = df['Return_1d'].shift(1)
    df['Return_1d_Lag2'] = df['Return_1d'].shift(2)
    df['BB_Position_Lag1'] = df['BB_Position'].shift(1)
    df['BB_Position_Lag2'] = df['BB_Position'].shift(2)

    # --- Target ---
    df['Target_Return'] = close.pct_change(1).shift(-1)
    df['Volatility_20d'] = df['RVol_20d']

    return df


# =============================================================================
# V2 MODELS (exact replica)
# =============================================================================

def train_rf_v2(X, y):
    m = RandomForestRegressor(
        n_estimators=200, max_depth=6, min_samples_leaf=10,
        max_features='sqrt', random_state=42, n_jobs=-1
    )
    m.fit(X, y)
    return m

def train_xgb_v2(X, y):
    m = xgb.XGBRegressor(
        objective='reg:squarederror', n_estimators=200, max_depth=4,
        learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1
    )
    m.fit(X, y, verbose=False)
    return m

def predict_v2(models, X):
    rf, xgb_m = models
    return np.clip(0.8 * rf.predict(X) + 0.2 * xgb_m.predict(X), -0.08, 0.08)


# =============================================================================
# V3 MODELS (stacking ensemble)
# =============================================================================

def train_lgbm_v3(X_tr, y_tr, X_val, y_val):
    m = lgb.LGBMRegressor(
        n_estimators=1000, max_depth=5, learning_rate=0.03,
        subsample=0.7, colsample_bytree=0.7, reg_alpha=0.3, reg_lambda=1.5,
        min_child_samples=20, random_state=42, verbose=-1, n_jobs=-1
    )
    m.fit(X_tr, y_tr, eval_set=[(X_val, y_val)],
          callbacks=[lgb.early_stopping(50, verbose=False)])
    return m

def train_xgb_v3(X_tr, y_tr, X_val, y_val):
    m = xgb.XGBRegressor(
        objective='reg:squarederror', n_estimators=1000, max_depth=4,
        learning_rate=0.03, subsample=0.7, colsample_bytree=0.7,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1,
        early_stopping_rounds=50
    )
    m.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
    return m

def train_rf_v3(X, y):
    m = RandomForestRegressor(
        n_estimators=300, max_depth=8, min_samples_leaf=15,
        max_features=0.5, random_state=42, n_jobs=-1
    )
    m.fit(X, y)
    return m


def build_stacking_ensemble(X_train, y_train, X_val, y_val):
    n_train = len(X_train)
    tscv = TimeSeriesSplit(n_splits=5)
    oof_preds = np.zeros((n_train, 3))  # lgbm, xgb, rf

    for fold_idx, (tr_idx, val_idx) in enumerate(tscv.split(X_train)):
        Xf_tr, yf_tr = X_train[tr_idx], y_train[tr_idx]
        Xf_val, yf_val = X_train[val_idx], y_train[val_idx]

        m_lgb = train_lgbm_v3(Xf_tr, yf_tr, Xf_val, yf_val)
        m_xgb = train_xgb_v3(Xf_tr, yf_tr, Xf_val, yf_val)
        m_rf  = train_rf_v3(Xf_tr, yf_tr)

        oof_preds[val_idx, 0] = m_lgb.predict(Xf_val)
        oof_preds[val_idx, 1] = m_xgb.predict(Xf_val)
        oof_preds[val_idx, 2] = m_rf.predict(Xf_val)

    # Train meta-learner on out-of-fold predictions (skip zeros from first folds)
    mask = np.any(oof_preds != 0, axis=1)
    meta = Ridge(alpha=0.01)
    meta.fit(oof_preds[mask], y_train[mask])

    # Retrain base models on full training data with early stopping on validation
    final_lgb = train_lgbm_v3(X_train, y_train, X_val, y_val)
    final_xgb = train_xgb_v3(X_train, y_train, X_val, y_val)
    final_rf  = train_rf_v3(X_train, y_train)

    return {'lgbm': final_lgb, 'xgb': final_xgb, 'rf': final_rf, 'meta': meta}


def predict_v3(ensemble, X):
    p_lgb = ensemble['lgbm'].predict(X)
    p_xgb = ensemble['xgb'].predict(X)
    p_rf  = ensemble['rf'].predict(X)
    stacked = np.column_stack([p_lgb, p_xgb, p_rf])
    pred = ensemble['meta'].predict(stacked)
    return np.clip(pred, -0.08, 0.08)


# =============================================================================
# WALK-FORWARD BACKTEST ENGINE
# =============================================================================

def walk_forward_backtest(df, feature_cols, version, backtest_start, initial_cash=10000):
    """
    version: 'v2' or 'v3' — determines models, retrain freq, threshold mode.
    """
    retrain_freq = RETRAIN_FREQ_V2 if version == 'v2' else RETRAIN_FREQ_V3
    threshold_fixed = (version == 'v2')

    # Prepare clean data
    df_clean = df.dropna(subset=['Target_Return'] + feature_cols).copy()

    # Find backtest start index
    bt_mask = df_clean.index >= pd.Timestamp(backtest_start)
    if bt_mask.sum() == 0:
        raise ValueError(f"No data after {backtest_start}")

    bt_start_idx = np.argmax(bt_mask)  # first True index
    all_X = df_clean[feature_cols].values
    all_y = df_clean['Target_Return'].values
    all_prices = df_clean['Close'].values.ravel()
    all_dates = df_clean.index
    all_vol = df_clean['Volatility_20d'].values

    cash = initial_cash
    shares = 0.0
    portfolio = []
    predictions = []
    actuals = []
    dates_out = []
    model = None
    scaler = StandardScaler()
    retrain_counter = 0

    total_test = len(all_X) - bt_start_idx - 1  # -1 because last row has no target
    print(f"  [{version.upper()}] Backtesting {total_test} days (retrain every {retrain_freq}d)...")

    for i in range(bt_start_idx, len(all_X) - 1):
        # Retrain check
        if model is None or retrain_counter >= retrain_freq:
            train_end = i
            X_tr_all = all_X[:train_end]
            y_tr_all = all_y[:train_end]

            val_split = int(len(X_tr_all) * 0.85)
            X_tr = X_tr_all[:val_split]
            y_tr = y_tr_all[:val_split]
            X_val = X_tr_all[val_split:]
            y_val = y_tr_all[val_split:]

            scaler.fit(X_tr_all)
            X_tr_s = scaler.transform(X_tr)
            X_val_s = scaler.transform(X_val)

            if version == 'v2':
                rf = train_rf_v2(scaler.transform(X_tr_all), y_tr_all)
                xgb_m = train_xgb_v2(scaler.transform(X_tr_all), y_tr_all)
                model = (rf, xgb_m)
            else:
                model = build_stacking_ensemble(X_tr_s, y_tr, X_val_s, y_val)

            retrain_counter = 0

        # Predict
        X_today = scaler.transform(all_X[i:i+1])
        if version == 'v2':
            pred = predict_v2(model, X_today)[0]
        else:
            pred = predict_v3(model, X_today)[0]

        actual = all_y[i]
        price = float(all_prices[i])

        # Threshold
        if threshold_fixed:
            threshold = 0.003
        else:
            vol = all_vol[i] if not np.isnan(all_vol[i]) else 0.01
            threshold = max(0.1 * vol, 0.0005)

        # Trade
        if pred > threshold and cash > 0:
            shares = cash / price
            cash = 0
        elif pred < -threshold and shares > 0:
            cash = shares * price
            shares = 0

        portfolio.append(cash + shares * price)
        predictions.append(pred)
        actuals.append(actual)
        dates_out.append(all_dates[i])
        retrain_counter += 1

    return {
        'dates': dates_out,
        'portfolio': np.array(portfolio),
        'predictions': np.array(predictions),
        'actuals': np.array(actuals),
        'prices': all_prices[bt_start_idx:bt_start_idx + len(portfolio)],
    }


# =============================================================================
# METRICS
# =============================================================================

def compute_metrics(portfolio, prices, initial_cash, predictions, actuals):
    total_ret = (portfolio[-1] / initial_cash - 1) * 100
    n_years = len(portfolio) / 252
    annual_ret = ((portfolio[-1] / initial_cash) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

    daily_rets = np.diff(portfolio) / portfolio[:-1]
    sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252)) if np.std(daily_rets) > 0 else 0

    peak = np.maximum.accumulate(portfolio)
    dd = (portfolio - peak) / peak
    max_dd = float(dd.min()) * 100

    dir_acc = np.mean(np.sign(predictions) == np.sign(actuals)) * 100
    mae = mean_absolute_error(actuals, predictions) * 100
    rmse = np.sqrt(mean_squared_error(actuals, predictions)) * 100

    # Count trades (transitions between cash and invested)
    invested = portfolio > 0  # simplification
    trades = 0
    was_cash = True
    for i, p in enumerate(portfolio):
        # approximate: look at portfolio changes
        pass
    # Better: count sign changes in position
    pos = np.array([1 if p > initial_cash * 0.5 else 0 for p in portfolio])  # rough
    trades = int(np.sum(np.abs(np.diff(np.concatenate([[0], (np.diff(portfolio) != 0).astype(int)])))))

    return {
        'total_return': round(total_ret, 2),
        'annual_return': round(annual_ret, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 1),
        'direction_accuracy': round(dir_acc, 1),
        'mae_pct': round(mae, 3),
        'rmse_pct': round(rmse, 3),
        'n_days': len(portfolio),
    }


def compute_bnh_metrics(prices, initial_cash):
    bnh = initial_cash * (prices / prices[0])
    total_ret = (bnh[-1] / initial_cash - 1) * 100
    n_years = len(bnh) / 252
    annual_ret = ((bnh[-1] / initial_cash) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

    daily_rets = np.diff(bnh) / bnh[:-1]
    sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252)) if np.std(daily_rets) > 0 else 0

    peak = np.maximum.accumulate(bnh)
    dd = (bnh - peak) / peak
    max_dd = float(dd.min()) * 100

    return {
        'total_return': round(total_ret, 2),
        'annual_return': round(annual_ret, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 1),
        'direction_accuracy': None,
        'mae_pct': None,
        'rmse_pct': None,
        'n_days': len(bnh),
        'equity': bnh,
    }


# =============================================================================
# CHART
# =============================================================================

def plot_comparison(dates, v2_portfolio, v3_portfolio, bnh_equity, output_path):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True,
                                    gridspec_kw={'height_ratios': [3, 1]})

    dates_plot = pd.to_datetime(dates)

    # Equity curves
    ax1.plot(dates_plot, v2_portfolio, label='V2 (RF+XGB)', linewidth=1.2, color='#2196F3')
    ax1.plot(dates_plot, v3_portfolio, label='V3 Advanced (Stacking)', linewidth=1.2, color='#FF5722')
    ax1.plot(dates_plot, bnh_equity, label='Buy & Hold', linewidth=1.0, color='#9E9E9E', linestyle='--')
    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title('SPY Backtest Comparison: V2 vs V3 Advanced vs Buy & Hold')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # Drawdown
    for arr, label, color in [
        (v2_portfolio, 'V2', '#2196F3'),
        (v3_portfolio, 'V3', '#FF5722'),
        (bnh_equity, 'B&H', '#9E9E9E'),
    ]:
        peak = np.maximum.accumulate(arr)
        dd = (arr - peak) / peak * 100
        ax2.fill_between(dates_plot, dd, 0, alpha=0.3, color=color, label=label)
        ax2.plot(dates_plot, dd, linewidth=0.8, color=color)

    ax2.set_ylabel('Drawdown (%)')
    ax2.set_xlabel('Date')
    ax2.legend(loc='lower left', fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\n  Chart saved: {output_path}")


# =============================================================================
# MAIN COMPARISON
# =============================================================================

def run_comparison():
    print("=" * 65)
    print("  FINANCE MODEL V3 ADVANCED — BACKTEST COMPARISON")
    print("=" * 65)

    # Load data
    print("\n[1/5] Loading SPY data...")
    df_raw = load_data()
    print(f"  Loaded {len(df_raw)} rows: {df_raw.index[0].date()} to {df_raw.index[-1].date()}")

    # Engineer features
    print("\n[2/5] Engineering features...")
    df_v2 = engineer_features_v2(df_raw.copy())
    df_v3 = engineer_features_v3(df_raw.copy())
    print(f"  V2: {len(V2_FEATURE_COLS)} features")
    print(f"  V3: {len(V3_FEATURE_COLS)} features")

    # Run V2 backtest
    print(f"\n[3/5] Running V2 backtest (from {BACKTEST_START})...")
    res_v2 = walk_forward_backtest(df_v2, V2_FEATURE_COLS, 'v2', BACKTEST_START, INITIAL_CASH)

    # Run V3 backtest
    print(f"\n[4/5] Running V3 backtest (from {BACKTEST_START})...")
    res_v3 = walk_forward_backtest(df_v3, V3_FEATURE_COLS, 'v3', BACKTEST_START, INITIAL_CASH)

    # Compute metrics
    print("\n[5/5] Computing metrics...")
    m_v2 = compute_metrics(res_v2['portfolio'], res_v2['prices'], INITIAL_CASH,
                           res_v2['predictions'], res_v2['actuals'])
    m_v3 = compute_metrics(res_v3['portfolio'], res_v3['prices'], INITIAL_CASH,
                           res_v3['predictions'], res_v3['actuals'])
    m_bnh = compute_bnh_metrics(res_v2['prices'], INITIAL_CASH)

    # Print comparison table
    print(f"\n{'=' * 65}")
    print(f"  SPY BACKTEST COMPARISON: {BACKTEST_START} to {res_v2['dates'][-1].date()}")
    print(f"{'=' * 65}")
    print(f"")
    print(f"  {'':25s} {'V2 Model':>12s}  {'V3 Advanced':>12s}  {'Buy & Hold':>12s}")
    print(f"  {'─' * 58}")
    print(f"  {'Total Return':25s} {m_v2['total_return']:>+11.1f}%  {m_v3['total_return']:>+11.1f}%  {m_bnh['total_return']:>+11.1f}%")
    print(f"  {'Annual Return':25s} {m_v2['annual_return']:>+11.1f}%  {m_v3['annual_return']:>+11.1f}%  {m_bnh['annual_return']:>+11.1f}%")
    print(f"  {'Sharpe Ratio':25s} {m_v2['sharpe']:>12.2f}  {m_v3['sharpe']:>12.2f}  {m_bnh['sharpe']:>12.2f}")
    print(f"  {'Max Drawdown':25s} {m_v2['max_drawdown']:>11.1f}%  {m_v3['max_drawdown']:>11.1f}%  {m_bnh['max_drawdown']:>11.1f}%")
    da_v2 = f"{m_v2['direction_accuracy']:.1f}%" if m_v2['direction_accuracy'] else "—"
    da_v3 = f"{m_v3['direction_accuracy']:.1f}%" if m_v3['direction_accuracy'] else "—"
    print(f"  {'Direction Accuracy':25s} {da_v2:>12s}  {da_v3:>12s}  {'—':>12s}")
    mae_v2 = f"{m_v2['mae_pct']:.3f}%" if m_v2['mae_pct'] else "—"
    mae_v3 = f"{m_v3['mae_pct']:.3f}%" if m_v3['mae_pct'] else "—"
    print(f"  {'Pred MAE (%)':25s} {mae_v2:>12s}  {mae_v3:>12s}  {'—':>12s}")
    rmse_v2 = f"{m_v2['rmse_pct']:.3f}%" if m_v2['rmse_pct'] else "—"
    rmse_v3 = f"{m_v3['rmse_pct']:.3f}%" if m_v3['rmse_pct'] else "—"
    print(f"  {'Pred RMSE (%)':25s} {rmse_v2:>12s}  {rmse_v3:>12s}  {'—':>12s}")
    print(f"  {'Test Days':25s} {m_v2['n_days']:>12d}  {m_v3['n_days']:>12d}  {m_bnh['n_days']:>12d}")
    print(f"  {'─' * 58}")
    print(f"")
    print(f"  V3 Features: {len(V3_FEATURE_COLS)}  |  V2 Features: {len(V2_FEATURE_COLS)}")
    print(f"  V3 Retrain: every {RETRAIN_FREQ_V3}d  |  V2 Retrain: every {RETRAIN_FREQ_V2}d")
    print(f"  V3 Threshold: dynamic (0.5x vol)  |  V2 Threshold: fixed 0.3%")

    # Chart
    min_len = min(len(res_v2['portfolio']), len(res_v3['portfolio']))
    dates_common = res_v2['dates'][:min_len]
    v2_eq = res_v2['portfolio'][:min_len]
    v3_eq = res_v3['portfolio'][:min_len]
    bnh_eq = m_bnh['equity'][:min_len]

    plot_comparison(dates_common, v2_eq, v3_eq, bnh_eq, OUTPUT_CHART)

    print(f"\n{'=' * 65}")
    print(f"  DONE")
    print(f"{'=' * 65}")


if __name__ == '__main__':
    run_comparison()
