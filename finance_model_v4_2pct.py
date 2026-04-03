"""
Quantfolio Backtest — Lite vs Pro with Statistical Analysis
=============================================================
Strategy rules:
  1. If model predicts next-day return >= +2%, BUY (go fully invested)
  2. If model predicts next-day return <= -2%, SELL (go fully cash)
  3. Otherwise, HOLD current position

Improvements:
  - Pro features trimmed from 37 to 22 (removed redundant/noisy features)
  - Bootstrap Sharpe ratio confidence intervals (10,000 resamples)
  - Threshold sensitivity analysis across 0.5% to 5.0%

Compares Lite (RF+XGB) vs Pro (Stacking Ensemble) vs Buy & Hold.

Usage:
  python finance_model_v4_2pct.py
"""
import sys

import pandas as pd
import numpy as np
import warnings
import os
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
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)
os.environ['PYTHONWARNINGS'] = 'ignore'
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

# =============================================================================
# CONFIGURATION
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(SCRIPT_DIR, "data_cache", "SPY.csv")
BACKTEST_START = '2015-01-02'
RETRAIN_FREQ_V2 = 63   # aligned with Z-score lookback (was 20, caused stale cash positions)
RETRAIN_FREQ_V3 = 63
INITIAL_CASH = 10000
THRESHOLD = 2.5  # Z-score threshold (±2.5 sigma — optimal per sensitivity analysis)
OUTPUT_CHART = os.path.join(SCRIPT_DIR, "v4_2pct_backtest_comparison.png")


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
# V2 FEATURE ENGINEERING
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
# V3 FEATURE ENGINEERING (22 features — trimmed from 37)
# =============================================================================

V3_FEATURE_COLS = [
    # Core price structure (9)
    'Dist_SMA50', 'Dist_SMA200', 'RSI', 'BB_Position',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d', 'SMA_Cross',
    # Volume (3)
    'Volume_Ratio_20d', 'OBV_Slope_10d', 'Volume_Zscore_20d',
    # Multi-timeframe momentum (2)
    'ROC_10d', 'ROC_60d',
    # Volatility (2)
    'ATR_Norm', 'GK_Vol_20d',
    # Mean reversion (1)
    'Zscore_50d',
    # Trend strength (2)
    'ADX_14', 'MACD_Hist_Norm',
    # Lagged (3)
    'RSI_Lag1', 'Return_1d_Lag1', 'BB_Position_Lag1',
]

def engineer_features_v3(df):
    close = df['Close'].squeeze()
    high = df['High'].squeeze()
    low = df['Low'].squeeze()
    opn = df['Open'].squeeze()
    volume = df['Volume'].squeeze().astype(float)

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

    vol_sma20 = volume.rolling(20).mean()
    df['Volume_Ratio_20d'] = volume / vol_sma20
    obv = OnBalanceVolumeIndicator(close, volume).on_balance_volume()
    obv_series = obv.rolling(10).apply(
        lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 10 else 0, raw=True
    )
    df['OBV_Slope_10d'] = obv_series / (volume.rolling(10).mean() + 1e-10)
    price_dir = np.sign(close.pct_change(5))
    vol_dir = np.sign(volume.pct_change(5))
    df['Volume_Price_Div'] = (price_dir != vol_dir).astype(float)
    vol_std20 = volume.rolling(20).std()
    df['Volume_Zscore_20d'] = (volume - vol_sma20) / (vol_std20 + 1e-10)

    df['ROC_3d']  = ROCIndicator(close, window=3).roc()
    df['ROC_10d'] = ROCIndicator(close, window=10).roc()
    df['ROC_20d'] = ROCIndicator(close, window=20).roc()
    df['ROC_60d'] = ROCIndicator(close, window=60).roc()

    atr = AverageTrueRange(high, low, close, window=14).average_true_range()
    df['ATR_Norm'] = atr / close
    log_hl = np.log(high / low) ** 2
    log_co = np.log(close / opn) ** 2
    gk = 0.5 * log_hl - (2 * np.log(2) - 1) * log_co
    df['GK_Vol_20d'] = gk.rolling(20).mean()
    df['Intraday_Range'] = (high - low) / close

    sma20 = SMAIndicator(close, window=20).sma_indicator()
    std20 = close.rolling(20).std()
    std50 = close.rolling(50).std()
    df['Zscore_20d'] = (close - sma20) / (std20 + 1e-10)
    df['Zscore_50d'] = (close - sma50) / (std50 + 1e-10)

    df['ADX_14'] = ADXIndicator(high, low, close, window=14).adx()
    macd = MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df['MACD_Hist_Norm'] = macd.macd_diff() / close

    df['Day_of_Week'] = df.index.dayofweek.astype(float)
    df['Month'] = df.index.month.astype(float)
    qe = pd.Series(df.index, index=df.index).apply(
        lambda d: 1.0 if d.month in [3, 6, 9, 12] and d.day >= 25 else 0.0
    )
    df['Quarter_End'] = qe.values

    df['RSI_Lag1'] = rsi.shift(1)
    df['RSI_Lag2'] = rsi.shift(2)
    df['Return_1d_Lag1'] = df['Return_1d'].shift(1)
    df['Return_1d_Lag2'] = df['Return_1d'].shift(2)
    df['BB_Position_Lag1'] = df['BB_Position'].shift(1)
    df['BB_Position_Lag2'] = df['BB_Position'].shift(2)

    df['Target_Return'] = close.pct_change(1).shift(-1)
    df['Volatility_20d'] = df['RVol_20d']
    return df


# =============================================================================
# V2 MODELS
# =============================================================================

def train_rf_v2(X, y):
    m = RandomForestRegressor(
        n_estimators=200, max_depth=6, min_samples_leaf=10,
        max_features='sqrt', random_state=42, n_jobs=1
    )
    m.fit(X, y)
    return m

def train_xgb_v2(X, y):
    m = xgb.XGBRegressor(
        objective='reg:squarederror', n_estimators=200, max_depth=4,
        learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1
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
        min_child_samples=20, random_state=42, verbose=-1, n_jobs=1
    )
    m.fit(X_tr, y_tr, eval_set=[(X_val, y_val)],
          callbacks=[lgb.early_stopping(50, verbose=False)])
    return m

def train_xgb_v3(X_tr, y_tr, X_val, y_val):
    m = xgb.XGBRegressor(
        objective='reg:squarederror', n_estimators=1000, max_depth=4,
        learning_rate=0.03, subsample=0.7, colsample_bytree=0.7,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1,
        early_stopping_rounds=50
    )
    m.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
    return m

def train_rf_v3(X, y):
    m = RandomForestRegressor(
        n_estimators=300, max_depth=8, min_samples_leaf=15,
        max_features=0.5, random_state=42, n_jobs=1
    )
    m.fit(X, y)
    return m

def build_stacking_ensemble(X_train, y_train, X_val, y_val):
    """
    Build ensemble with OOF-optimized weights instead of Ridge meta-learner.
    Ridge was compressing predictions to near-zero, making them useless for
    threshold-based trading. Simple weighted average preserves signal magnitude.
    """
    n_train = len(X_train)
    tscv = TimeSeriesSplit(n_splits=5)
    oof_preds = np.zeros((n_train, 3))

    for fold_idx, (tr_idx, val_idx) in enumerate(tscv.split(X_train)):
        Xf_tr, yf_tr = X_train[tr_idx], y_train[tr_idx]
        Xf_val, yf_val = X_train[val_idx], y_train[val_idx]

        m_lgb = train_lgbm_v3(Xf_tr, yf_tr, Xf_val, yf_val)
        m_xgb = train_xgb_v3(Xf_tr, yf_tr, Xf_val, yf_val)
        m_rf  = train_rf_v3(Xf_tr, yf_tr)

        oof_preds[val_idx, 0] = m_lgb.predict(Xf_val)
        oof_preds[val_idx, 1] = m_xgb.predict(Xf_val)
        oof_preds[val_idx, 2] = m_rf.predict(Xf_val)

    # Compute OOF-optimized weights (inverse MAE weighting)
    mask = np.any(oof_preds != 0, axis=1)
    oof_valid = oof_preds[mask]
    y_valid = y_train[mask]
    mae_per_model = np.array([
        np.mean(np.abs(oof_valid[:, j] - y_valid)) for j in range(3)
    ])
    inv_mae = 1.0 / (mae_per_model + 1e-10)
    weights = inv_mae / inv_mae.sum()

    # Retrain final models on full training data
    final_lgb = train_lgbm_v3(X_train, y_train, X_val, y_val)
    final_xgb = train_xgb_v3(X_train, y_train, X_val, y_val)
    final_rf  = train_rf_v3(X_train, y_train)

    return {'lgbm': final_lgb, 'xgb': final_xgb, 'rf': final_rf, 'weights': weights}

def predict_v3(ensemble, X):
    p_lgb = ensemble['lgbm'].predict(X)
    p_xgb = ensemble['xgb'].predict(X)
    p_rf  = ensemble['rf'].predict(X)
    w = ensemble['weights']
    pred = w[0] * p_lgb + w[1] * p_xgb + w[2] * p_rf
    return pred  # no clipping — preserve full signal range


# =============================================================================
# WALK-FORWARD BACKTEST — Z-SCORE SIGNAL WITH HOLD
# =============================================================================

def walk_forward_backtest_2pct(df, feature_cols, version, backtest_start,
                                initial_cash=10000, threshold=2.0):
    """
    Z-score signal strategy:
      Instead of comparing raw predictions to an absolute ±2% threshold
      (which fails because ML predictions are compressed near zero),
      we convert each prediction to a Z-score relative to a rolling window
      of recent predictions. This asks: "Is today's prediction unusually
      bullish or bearish compared to what the model normally predicts?"

      Z-score >= +threshold  → BUY  (unusually bullish prediction)
      Z-score <= -threshold  → SELL (unusually bearish prediction)
      otherwise              → HOLD

      Default threshold=2.0 means buy/sell on ~2-sigma signals (~5% of days).
    """
    retrain_freq = RETRAIN_FREQ_V2 if version == 'v2' else RETRAIN_FREQ_V3
    ZSCORE_LOOKBACK = 126  # ~6 months of trading days for rolling stats

    df_clean = df.dropna(subset=['Target_Return'] + feature_cols).copy()
    bt_mask = df_clean.index >= pd.Timestamp(backtest_start)
    if bt_mask.sum() == 0:
        raise ValueError(f"No data after {backtest_start}")

    bt_start_idx = np.argmax(bt_mask)
    all_X = df_clean[feature_cols].values
    all_y = df_clean['Target_Return'].values
    all_prices = df_clean['Close'].values.ravel()
    all_dates = df_clean.index

    cash = initial_cash
    shares = 0.0
    portfolio = []
    raw_predictions = []
    z_scores = []
    actuals = []
    dates_out = []
    signals = []
    model = None
    scaler = StandardScaler()
    retrain_counter = 0
    pred_history = []  # rolling window of recent predictions

    total_test = len(all_X) - bt_start_idx - 1
    print(f"  [{version.upper()}] Backtesting {total_test} days "
          f"(retrain every {retrain_freq}d, Z-score threshold=±{threshold:.1f})...")

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

            # Seed prediction history with validation predictions
            if not pred_history:
                X_val_scaled = scaler.transform(X_val)
                if version == 'v2':
                    seed_preds = predict_v2(model, X_val_scaled)
                else:
                    seed_preds = predict_v3(model, X_val_scaled)
                pred_history = list(seed_preds[-ZSCORE_LOOKBACK:])

            retrain_counter = 0

        # Predict
        X_today = scaler.transform(all_X[i:i+1])
        if version == 'v2':
            raw_pred = predict_v2(model, X_today)[0]
        else:
            raw_pred = predict_v3(model, X_today)[0]

        # Compute Z-score relative to recent prediction history
        pred_history.append(raw_pred)
        if len(pred_history) > ZSCORE_LOOKBACK:
            pred_history = pred_history[-ZSCORE_LOOKBACK:]

        if len(pred_history) >= 20:  # need minimum history
            hist = np.array(pred_history[:-1])  # exclude current
            mu = np.mean(hist)
            sigma = np.std(hist)
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0  # not enough history yet

        price = float(all_prices[i])

        # ===== Z-SCORE THRESHOLD STRATEGY =====
        if z >= threshold and cash > 0:
            shares = cash / price
            cash = 0
            signals.append('BUY')
        elif z <= -threshold and shares > 0:
            cash = shares * price
            shares = 0
            signals.append('SELL')
        else:
            signals.append('HOLD')

        portfolio.append(cash + shares * price)
        raw_predictions.append(raw_pred)
        z_scores.append(z)
        actuals.append(all_y[i])
        dates_out.append(all_dates[i])
        retrain_counter += 1

    # Count signals
    n_buy = signals.count('BUY')
    n_sell = signals.count('SELL')
    n_hold = signals.count('HOLD')
    zs = np.array(z_scores)
    print(f"  [{version.upper()}] Signals: {n_buy} BUY, {n_sell} SELL, {n_hold} HOLD "
          f"({n_buy+n_sell} trades out of {len(signals)} days)")
    print(f"  [{version.upper()}] Z-score range: [{zs.min():.2f}, {zs.max():.2f}], "
          f"std={zs.std():.2f}")

    return {
        'dates': dates_out,
        'portfolio': np.array(portfolio),
        'predictions': np.array(raw_predictions),
        'rescaled_preds': zs,  # Z-scores (used by sensitivity analysis)
        'actuals': np.array(actuals),
        'prices': all_prices[bt_start_idx:bt_start_idx + len(portfolio)],
        'signals': signals,
        'n_buy': n_buy,
        'n_sell': n_sell,
        'n_hold': n_hold,
    }


# =============================================================================
# METRICS
# =============================================================================

def compute_metrics(portfolio, prices, initial_cash, z_scores, actuals,
                    predictions=None, n_buy=0, n_sell=0, n_hold=0, signals=None):
    total_ret = (portfolio[-1] / initial_cash - 1) * 100
    n_years = len(portfolio) / 252
    annual_ret = ((portfolio[-1] / initial_cash) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

    daily_rets = np.diff(portfolio) / portfolio[:-1]
    sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252)) if np.std(daily_rets) > 0 else 0

    peak = np.maximum.accumulate(portfolio)
    dd = (portfolio - peak) / peak
    max_dd = float(dd.min()) * 100

    # Direction accuracy based on raw predictions (positive Z = predicting up)
    dir_acc = np.mean(np.sign(z_scores) == np.sign(actuals)) * 100

    # MAE/RMSE from raw predictions vs actual returns
    if predictions is not None:
        mae = float(np.mean(np.abs(predictions - actuals))) * 100
        rmse = float(np.sqrt(np.mean((predictions - actuals) ** 2))) * 100
    else:
        mae = 0.0
        rmse = 0.0

    # Win rate: when model signals BUY, was next-day return positive?
    acts_arr = np.array(actuals)
    if signals is not None:
        buy_indices = [i for i, s in enumerate(signals) if s == 'BUY']
        sell_indices = [i for i, s in enumerate(signals) if s == 'SELL']
        buy_win = np.mean(acts_arr[buy_indices] > 0) * 100 if len(buy_indices) > 0 else 0
        sell_win = np.mean(acts_arr[sell_indices] < 0) * 100 if len(sell_indices) > 0 else 0
    else:
        buy_win = 0
        sell_win = 0

    return {
        'total_return': round(total_ret, 2),
        'annual_return': round(annual_ret, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 1),
        'direction_accuracy': round(dir_acc, 1),
        'mae_pct': round(mae, 3),
        'rmse_pct': round(rmse, 3),
        'n_days': len(portfolio),
        'n_buy': n_buy,
        'n_sell': n_sell,
        'n_hold': n_hold,
        'n_trades': n_buy + n_sell,
        'buy_win_rate': round(buy_win, 1),
        'sell_win_rate': round(sell_win, 1),
        'final_value': round(portfolio[-1], 2),
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
        'n_days': len(bnh),
        'final_value': round(bnh[-1], 2),
        'equity': bnh,
    }


# =============================================================================
# BOOTSTRAP SHARPE CONFIDENCE INTERVALS
# =============================================================================

def bootstrap_sharpe(portfolio, n_bootstrap=10000, ci_level=0.95, seed=42):
    """
    Compute Sharpe ratio with bootstrap confidence interval.
    Returns: (sharpe, lower_bound, upper_bound)
    """
    daily_rets = np.diff(portfolio) / portfolio[:-1]
    n = len(daily_rets)
    if n < 30 or np.std(daily_rets) == 0:
        return 0.0, 0.0, 0.0

    observed_sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252))

    rng = np.random.RandomState(seed)
    boot_sharpes = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        sample = rng.choice(daily_rets, size=n, replace=True)
        s = np.std(sample)
        boot_sharpes[b] = np.mean(sample) / s * np.sqrt(252) if s > 0 else 0.0

    alpha = (1 - ci_level) / 2
    lo = np.percentile(boot_sharpes, alpha * 100)
    hi = np.percentile(boot_sharpes, (1 - alpha) * 100)
    return observed_sharpe, lo, hi


# =============================================================================
# THRESHOLD SENSITIVITY ANALYSIS (reuses existing predictions — no retraining)
# =============================================================================

def threshold_sensitivity(rescaled_preds, actuals, prices, initial_cash,
                          thresholds=None):
    """
    Replay the trading strategy at different thresholds using the SAME
    rescaled predictions from the main backtest. No model retraining needed.
    This is fast and gives a fair comparison across thresholds.
    """
    if thresholds is None:
        thresholds = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]

    results = []
    for thr in thresholds:
        # Replay trading with this threshold
        cash = initial_cash
        shares = 0.0
        portfolio = []
        n_buy = 0
        n_sell = 0

        for i in range(len(rescaled_preds)):
            pred = rescaled_preds[i]
            price = float(prices[i])

            if pred >= thr and cash > 0:
                shares = cash / price
                cash = 0
                n_buy += 1
            elif pred <= -thr and shares > 0:
                cash = shares * price
                shares = 0
                n_sell += 1

            portfolio.append(cash + shares * price)

        port = np.array(portfolio)
        daily_rets = np.diff(port) / port[:-1]
        sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252)) if np.std(daily_rets) > 0 else 0
        total_ret = (port[-1] / initial_cash - 1) * 100
        annual_ret = ((port[-1] / initial_cash) ** (1 / (len(port) / 252)) - 1) * 100
        peak = np.maximum.accumulate(port)
        max_dd = float(((port - peak) / peak).min()) * 100
        _, sh_lo, sh_hi = bootstrap_sharpe(port)

        results.append({
            'threshold': thr,
            'threshold_pct': f"±{thr:.1f}σ",
            'total_return': round(total_ret, 1),
            'annual_return': round(annual_ret, 1),
            'sharpe': round(sharpe, 2),
            'sharpe_ci_lo': round(sh_lo, 2),
            'sharpe_ci_hi': round(sh_hi, 2),
            'max_drawdown': round(max_dd, 1),
            'n_trades': n_buy + n_sell,
            'final_value': round(port[-1], 2),
        })

        print(f"    ±{thr:.1f}σ: Sharpe={sharpe:.2f}, Return={total_ret:+.1f}%, "
              f"Trades={n_buy+n_sell}, MaxDD={max_dd:.1f}%")

    return pd.DataFrame(results)


# =============================================================================
# CHART
# =============================================================================

def plot_comparison(dates, v2_portfolio, v3_portfolio, bnh_equity, output_path,
                    v2_signals=None, v3_signals=None):
    fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True,
                              gridspec_kw={'height_ratios': [3, 1, 1]})
    ax1, ax2, ax3 = axes
    dates_plot = pd.to_datetime(dates)

    # --- Panel 1: Equity curves ---
    ax1.plot(dates_plot, v2_portfolio, label='Lite (RF+XGB)', linewidth=1.3, color='#2196F3')
    ax1.plot(dates_plot, v3_portfolio, label='Pro (Stacking)', linewidth=1.3, color='#FF5722')
    ax1.plot(dates_plot, bnh_equity, label='Buy & Hold', linewidth=1.0, color='#9E9E9E', linestyle='--')

    # Mark BUY/SELL signals for V3
    if v3_signals is not None:
        for i, sig in enumerate(v3_signals):
            if sig == 'BUY':
                ax1.axvline(dates_plot[i], alpha=0.15, color='green', linewidth=0.5)
            elif sig == 'SELL':
                ax1.axvline(dates_plot[i], alpha=0.15, color='red', linewidth=0.5)

    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title('SPY Backtest: Lite vs Pro vs Buy & Hold')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # --- Panel 2: Drawdown ---
    for arr, label, color in [
        (v2_portfolio, 'Lite', '#2196F3'),
        (v3_portfolio, 'Pro', '#FF5722'),
        (bnh_equity, 'B&H', '#9E9E9E'),
    ]:
        peak = np.maximum.accumulate(arr)
        dd = (arr - peak) / peak * 100
        ax2.fill_between(dates_plot, dd, 0, alpha=0.3, color=color, label=label)
        ax2.plot(dates_plot, dd, linewidth=0.8, color=color)

    ax2.set_ylabel('Drawdown (%)')
    ax2.legend(loc='lower left', fontsize=8)
    ax2.grid(True, alpha=0.3)

    # --- Panel 3: Position status (V3) ---
    if v3_signals is not None:
        position = []
        pos = 0  # 0=cash, 1=invested
        for sig in v3_signals:
            if sig == 'BUY':
                pos = 1
            elif sig == 'SELL':
                pos = 0
            position.append(pos)
        ax3.fill_between(dates_plot, position, 0, alpha=0.4, color='#4CAF50', label='Pro Invested')
        ax3.set_ylabel('Pro Position')
        ax3.set_yticks([0, 1])
        ax3.set_yticklabels(['Cash', 'Invested'])
        ax3.legend(loc='upper left', fontsize=8)
        ax3.grid(True, alpha=0.3)

    ax3.set_xlabel('Date')
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\n  Chart saved: {output_path}")


# =============================================================================
# MAIN
# =============================================================================

def run_comparison():
    print("=" * 70)
    print("  QUANTFOLIO — BACKTEST WITH STATISTICAL ANALYSIS")
    print("=" * 70)
    print(f"  Strategy: BUY if Z-score >= +{THRESHOLD}, SELL if Z-score <= -{THRESHOLD}, else HOLD")
    print(f"  Starting capital: ${INITIAL_CASH:,}")
    print(f"  Pro model: 22 features (trimmed from 37)")

    # Load data
    print(f"\n[1/7] Loading SPY data...")
    df_raw = load_data()
    print(f"  Loaded {len(df_raw)} rows: {df_raw.index[0].date()} to {df_raw.index[-1].date()}")

    # Engineer features
    print(f"\n[2/7] Engineering features...")
    df_v2 = engineer_features_v2(df_raw.copy())
    df_v3 = engineer_features_v3(df_raw.copy())
    print(f"  Lite: {len(V2_FEATURE_COLS)} features  |  Pro: {len(V3_FEATURE_COLS)} features")

    # Run Lite backtest
    print(f"\n[3/7] Running Lite backtest (±2% threshold)...")
    res_v2 = walk_forward_backtest_2pct(df_v2, V2_FEATURE_COLS, 'v2',
                                         BACKTEST_START, INITIAL_CASH, THRESHOLD)

    # Run Pro backtest
    print(f"\n[4/7] Running Pro backtest (±2% threshold)...")
    res_v3 = walk_forward_backtest_2pct(df_v3, V3_FEATURE_COLS, 'v3',
                                         BACKTEST_START, INITIAL_CASH, THRESHOLD)

    # Compute metrics
    print(f"\n[5/7] Computing metrics & bootstrap confidence intervals...")
    m_v2 = compute_metrics(res_v2['portfolio'], res_v2['prices'], INITIAL_CASH,
                           res_v2['rescaled_preds'], res_v2['actuals'],
                           predictions=res_v2['predictions'],
                           n_buy=res_v2['n_buy'], n_sell=res_v2['n_sell'],
                           n_hold=res_v2['n_hold'], signals=res_v2['signals'])
    m_v3 = compute_metrics(res_v3['portfolio'], res_v3['prices'], INITIAL_CASH,
                           res_v3['rescaled_preds'], res_v3['actuals'],
                           predictions=res_v3['predictions'],
                           n_buy=res_v3['n_buy'], n_sell=res_v3['n_sell'],
                           n_hold=res_v3['n_hold'], signals=res_v3['signals'])
    m_bnh = compute_bnh_metrics(res_v2['prices'], INITIAL_CASH)

    # Bootstrap Sharpe CIs
    sh_v2, sh_v2_lo, sh_v2_hi = bootstrap_sharpe(res_v2['portfolio'])
    sh_v3, sh_v3_lo, sh_v3_hi = bootstrap_sharpe(res_v3['portfolio'])
    sh_bnh, sh_bnh_lo, sh_bnh_hi = bootstrap_sharpe(m_bnh['equity'])

    # Print comparison table
    print(f"\n{'=' * 70}")
    print(f"  SPY BACKTEST: {BACKTEST_START} -> {res_v2['dates'][-1].date()}")
    print(f"{'=' * 70}")
    print()
    print(f"  {'':25s} {'Lite':>12s}  {'Pro':>12s}  {'Buy & Hold':>12s}")
    print(f"  {'─' * 62}")
    print(f"  {'Final Value':25s} {'$'+str(m_v2['final_value']):>12s}  {'$'+str(m_v3['final_value']):>12s}  {'$'+str(m_bnh['final_value']):>12s}")
    print(f"  {'Total Return':25s} {m_v2['total_return']:>+11.1f}%  {m_v3['total_return']:>+11.1f}%  {m_bnh['total_return']:>+11.1f}%")
    print(f"  {'Annual Return':25s} {m_v2['annual_return']:>+11.1f}%  {m_v3['annual_return']:>+11.1f}%  {m_bnh['annual_return']:>+11.1f}%")
    print(f"  {'Sharpe Ratio':25s} {m_v2['sharpe']:>12.2f}  {m_v3['sharpe']:>12.2f}  {m_bnh['sharpe']:>12.2f}")
    print(f"  {'  95% CI':25s} [{sh_v2_lo:+.2f},{sh_v2_hi:+.2f}]  [{sh_v3_lo:+.2f},{sh_v3_hi:+.2f}]  [{sh_bnh_lo:+.2f},{sh_bnh_hi:+.2f}]")
    print(f"  {'Max Drawdown':25s} {m_v2['max_drawdown']:>11.1f}%  {m_v3['max_drawdown']:>11.1f}%  {m_bnh['max_drawdown']:>11.1f}%")
    print(f"  {'Direction Accuracy':25s} {m_v2['direction_accuracy']:>11.1f}%  {m_v3['direction_accuracy']:>11.1f}%  {'--':>12s}")
    print(f"  {'─' * 62}")
    print(f"  {'BUY Signals':25s} {m_v2['n_buy']:>12d}  {m_v3['n_buy']:>12d}  {'--':>12s}")
    print(f"  {'SELL Signals':25s} {m_v2['n_sell']:>12d}  {m_v3['n_sell']:>12d}  {'--':>12s}")
    print(f"  {'HOLD Days':25s} {m_v2['n_hold']:>12d}  {m_v3['n_hold']:>12d}  {'--':>12s}")
    print(f"  {'Total Trades':25s} {m_v2['n_trades']:>12d}  {m_v3['n_trades']:>12d}  {'0':>12s}")
    print(f"  {'BUY Win Rate':25s} {m_v2['buy_win_rate']:>11.1f}%  {m_v3['buy_win_rate']:>11.1f}%  {'--':>12s}")
    print(f"  {'SELL Win Rate':25s} {m_v2['sell_win_rate']:>11.1f}%  {m_v3['sell_win_rate']:>11.1f}%  {'--':>12s}")
    print(f"  {'─' * 62}")
    print(f"  {'Test Days':25s} {m_v2['n_days']:>12d}  {m_v3['n_days']:>12d}  {m_bnh['n_days']:>12d}")

    # Interpretation
    print(f"\n  STATISTICAL SIGNIFICANCE:")
    if sh_v3_lo > 0:
        print(f"  Pro Sharpe CI [{sh_v3_lo:+.2f}, {sh_v3_hi:+.2f}] excludes zero -> statistically significant")
    else:
        print(f"  Pro Sharpe CI [{sh_v3_lo:+.2f}, {sh_v3_hi:+.2f}] includes zero -> NOT statistically significant")
    if sh_v2_lo > 0:
        print(f"  Lite Sharpe CI [{sh_v2_lo:+.2f}, {sh_v2_hi:+.2f}] excludes zero -> statistically significant")
    else:
        print(f"  Lite Sharpe CI [{sh_v2_lo:+.2f}, {sh_v2_hi:+.2f}] includes zero -> NOT statistically significant")

    # Chart
    print(f"\n[6/7] Generating charts...")
    min_len = min(len(res_v2['portfolio']), len(res_v3['portfolio']))
    dates_common = res_v2['dates'][:min_len]
    v2_eq = res_v2['portfolio'][:min_len]
    v3_eq = res_v3['portfolio'][:min_len]
    bnh_eq = m_bnh['equity'][:min_len]
    v3_sigs = res_v3['signals'][:min_len]

    plot_comparison(dates_common, v2_eq, v3_eq, bnh_eq, OUTPUT_CHART,
                    v3_signals=v3_sigs)

    # Threshold sensitivity analysis (reuses Pro predictions — instant, no retraining)
    print(f"\n[7/7] Running threshold sensitivity analysis (Pro model)...", flush=True)
    try:
        sens_df = threshold_sensitivity(
            res_v3['rescaled_preds'], res_v3['actuals'],
            res_v3['prices'], INITIAL_CASH
        )
    except Exception as e:
        print(f"\n  ERROR in sensitivity analysis: {e}")
        import traceback; traceback.print_exc()
        print(f"\n{'=' * 70}\n  DONE (sensitivity analysis failed)\n{'=' * 70}")
        return
    if sens_df is None or sens_df.empty:
        print(f"  No sensitivity results returned.")
        return

    print(f"\n{'=' * 85}")
    print(f"  THRESHOLD SENSITIVITY ANALYSIS (Pro Model)")
    print(f"{'=' * 85}")
    print(f"  {'Threshold':>10s}  {'Return':>8s}  {'Annual':>8s}  {'Sharpe':>8s}  {'95% CI':>16s}  {'MaxDD':>8s}  {'Trades':>7s}  {'Final $':>10s}")
    print(f"  {'─' * 80}")
    for _, r in sens_df.iterrows():
        ci_str = f"[{r['sharpe_ci_lo']:+.2f},{r['sharpe_ci_hi']:+.2f}]"
        marker = " <-- current" if abs(r['threshold'] - THRESHOLD) < 1e-6 else ""
        print(f"  {r['threshold_pct']:>10s}  {r['total_return']:>+7.1f}%  {r['annual_return']:>+7.1f}%  "
              f"{r['sharpe']:>8.2f}  {ci_str:>16s}  {r['max_drawdown']:>7.1f}%  {r['n_trades']:>7d}  "
              f"${r['final_value']:>9,.0f}{marker}")
    print(f"  {'─' * 80}")

    # Find optimal threshold
    best = sens_df.loc[sens_df['sharpe'].idxmax()]
    print(f"\n  OPTIMAL THRESHOLD: {best['threshold_pct']} (Sharpe={best['sharpe']:.2f}, "
          f"Return={best['total_return']:+.1f}%, MaxDD={best['max_drawdown']:.1f}%)")
    if abs(best['threshold'] - THRESHOLD) > 0.05:
        print(f"  NOTE: Current ±{THRESHOLD:.1f}σ is not optimal. "
              f"Consider switching to {best['threshold_pct']}.")
    else:
        print(f"  Current ±{THRESHOLD:.1f}σ threshold IS the optimal choice.")

    # Save sensitivity results
    sens_path = os.path.join(SCRIPT_DIR, "threshold_sensitivity.csv")
    sens_df.to_csv(sens_path, index=False)
    print(f"  Sensitivity data saved: {sens_path}")

    print(f"\n{'=' * 70}")
    print(f"  DONE")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    run_comparison()
    sys.stdout.flush()
