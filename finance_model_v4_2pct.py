"""
Finance Model V4 — 2% Threshold Strategy Backtest
===================================================
Strategy rules:
  1. If model predicts next-day return >= +2%, BUY (go fully invested)
  2. If model predicts next-day return <= -2%, SELL (go fully cash)
  3. Otherwise, HOLD current position

Compares V2 (RF+XGB) vs V3 (Stacking Ensemble) vs Buy & Hold.

Usage:
  python finance_model_v4_2pct.py
"""

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
warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')
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
THRESHOLD = 0.02  # ±2% threshold
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
# V3 FEATURE ENGINEERING (37 features)
# =============================================================================

V3_FEATURE_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'Dist_EMA50', 'Dist_EMA200',
    'RSI', 'BB_Position', 'BB_Width',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d',
    'SMA_Cross', 'EMA_Cross',
    'Volume_Ratio_20d', 'OBV_Slope_10d', 'Volume_Price_Div', 'Volume_Zscore_20d',
    'ROC_3d', 'ROC_10d', 'ROC_20d', 'ROC_60d',
    'ATR_Norm', 'GK_Vol_20d', 'Intraday_Range',
    'Zscore_20d', 'Zscore_50d',
    'ADX_14', 'MACD_Hist_Norm',
    'Day_of_Week', 'Month', 'Quarter_End',
    'RSI_Lag1', 'RSI_Lag2', 'Return_1d_Lag1', 'Return_1d_Lag2',
    'BB_Position_Lag1', 'BB_Position_Lag2',
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

    mask = np.any(oof_preds != 0, axis=1)
    meta = Ridge(alpha=0.01)
    meta.fit(oof_preds[mask], y_train[mask])

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
# WALK-FORWARD BACKTEST — 2% THRESHOLD WITH HOLD
# =============================================================================

def walk_forward_backtest_2pct(df, feature_cols, version, backtest_start,
                                initial_cash=10000, threshold=0.02):
    """
    Strategy (with prediction rescaling):
      Models predict compressed returns (±0.1%). We rescale predictions so
      their std matches the actual return std from the training window.
      Then apply the ±2% threshold on the RESCALED prediction.

      rescaled_pred >= +threshold  → BUY  (go fully invested)
      rescaled_pred <= -threshold  → SELL (go fully to cash)
      otherwise                    → HOLD (keep current position)
    """
    retrain_freq = RETRAIN_FREQ_V2 if version == 'v2' else RETRAIN_FREQ_V3

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
    predictions = []
    rescaled_preds = []
    actuals = []
    dates_out = []
    signals = []          # 'BUY', 'SELL', 'HOLD'
    model = None
    scaler = StandardScaler()
    retrain_counter = 0
    pred_scale_factor = 1.0  # will be recalculated at each retrain

    total_test = len(all_X) - bt_start_idx - 1
    print(f"  [{version.upper()}] Backtesting {total_test} days "
          f"(retrain every {retrain_freq}d, threshold=±{threshold*100:.1f}%)...")

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

            # --- Compute rescaling factor ---
            # Generate predictions on recent training data to measure pred std
            recent_n = min(252, len(X_tr_all))  # last ~1 year
            X_recent = scaler.transform(X_tr_all[-recent_n:])
            if version == 'v2':
                cal_preds = predict_v2(model, X_recent)
            else:
                cal_preds = predict_v3(model, X_recent)

            pred_std = np.std(cal_preds)
            actual_std = np.std(y_tr_all[-recent_n:])
            # Scale predictions so they have same spread as actual returns
            pred_scale_factor = actual_std / pred_std if pred_std > 1e-10 else 1.0
            retrain_counter = 0

        # Predict
        X_today = scaler.transform(all_X[i:i+1])
        if version == 'v2':
            raw_pred = predict_v2(model, X_today)[0]
        else:
            raw_pred = predict_v3(model, X_today)[0]

        # Rescale prediction to match real return distribution
        pred = raw_pred * pred_scale_factor
        price = float(all_prices[i])

        # ===== 2% THRESHOLD STRATEGY =====
        if pred >= threshold and cash > 0:
            # BUY signal — go fully invested
            shares = cash / price
            cash = 0
            signals.append('BUY')
        elif pred <= -threshold and shares > 0:
            # SELL signal — go fully to cash
            cash = shares * price
            shares = 0
            signals.append('SELL')
        else:
            # HOLD — no action
            signals.append('HOLD')

        portfolio.append(cash + shares * price)
        predictions.append(raw_pred)
        rescaled_preds.append(pred)
        actuals.append(all_y[i])
        dates_out.append(all_dates[i])
        retrain_counter += 1

    # Count signals
    n_buy = signals.count('BUY')
    n_sell = signals.count('SELL')
    n_hold = signals.count('HOLD')
    rp = np.array(rescaled_preds)
    print(f"  [{version.upper()}] Signals: {n_buy} BUY, {n_sell} SELL, {n_hold} HOLD "
          f"({n_buy+n_sell} trades out of {len(signals)} days)")
    print(f"  [{version.upper()}] Rescaled pred range: [{rp.min():.4f}, {rp.max():.4f}], "
          f"std={rp.std():.4f}, scale_factor={pred_scale_factor:.1f}x")

    return {
        'dates': dates_out,
        'portfolio': np.array(portfolio),
        'predictions': np.array(predictions),
        'rescaled_preds': rp,
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

def compute_metrics(portfolio, prices, initial_cash, predictions, actuals,
                    n_buy=0, n_sell=0, n_hold=0):
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

    # Win rate: when model says BUY (pred >= threshold), was actual positive?
    preds_arr = np.array(predictions)
    acts_arr = np.array(actuals)
    buy_mask = preds_arr >= THRESHOLD
    sell_mask = preds_arr <= -THRESHOLD
    buy_win = np.mean(acts_arr[buy_mask] > 0) * 100 if buy_mask.sum() > 0 else 0
    sell_win = np.mean(acts_arr[sell_mask] < 0) * 100 if sell_mask.sum() > 0 else 0

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
# CHART
# =============================================================================

def plot_comparison(dates, v2_portfolio, v3_portfolio, bnh_equity, output_path,
                    v2_signals=None, v3_signals=None):
    fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True,
                              gridspec_kw={'height_ratios': [3, 1, 1]})
    ax1, ax2, ax3 = axes
    dates_plot = pd.to_datetime(dates)

    # --- Panel 1: Equity curves ---
    ax1.plot(dates_plot, v2_portfolio, label='V2 (RF+XGB)', linewidth=1.3, color='#2196F3')
    ax1.plot(dates_plot, v3_portfolio, label='V3 Advanced (Stacking)', linewidth=1.3, color='#FF5722')
    ax1.plot(dates_plot, bnh_equity, label='Buy & Hold', linewidth=1.0, color='#9E9E9E', linestyle='--')

    # Mark BUY/SELL signals for V3
    if v3_signals is not None:
        for i, sig in enumerate(v3_signals):
            if sig == 'BUY':
                ax1.axvline(dates_plot[i], alpha=0.15, color='green', linewidth=0.5)
            elif sig == 'SELL':
                ax1.axvline(dates_plot[i], alpha=0.15, color='red', linewidth=0.5)

    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title('SPY Backtest: ±2% Threshold Strategy — V2 vs V3 vs Buy & Hold')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # --- Panel 2: Drawdown ---
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
        ax3.fill_between(dates_plot, position, 0, alpha=0.4, color='#4CAF50', label='V3 Invested')
        ax3.set_ylabel('V3 Position')
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
    print("  FINANCE MODEL V4 — ±2% THRESHOLD STRATEGY BACKTEST")
    print("=" * 70)
    print(f"  Strategy: BUY if pred >= +2%, SELL if pred <= -2%, else HOLD")
    print(f"  Starting capital: ${INITIAL_CASH:,}")

    # Load data
    print(f"\n[1/5] Loading SPY data...")
    df_raw = load_data()
    print(f"  Loaded {len(df_raw)} rows: {df_raw.index[0].date()} to {df_raw.index[-1].date()}")

    # Engineer features
    print(f"\n[2/5] Engineering features...")
    df_v2 = engineer_features_v2(df_raw.copy())
    df_v3 = engineer_features_v3(df_raw.copy())
    print(f"  V2: {len(V2_FEATURE_COLS)} features  |  V3: {len(V3_FEATURE_COLS)} features")

    # Run V2 backtest
    print(f"\n[3/5] Running V2 backtest (±2% threshold)...")
    res_v2 = walk_forward_backtest_2pct(df_v2, V2_FEATURE_COLS, 'v2',
                                         BACKTEST_START, INITIAL_CASH, THRESHOLD)

    # Run V3 backtest
    print(f"\n[4/5] Running V3 backtest (±2% threshold)...")
    res_v3 = walk_forward_backtest_2pct(df_v3, V3_FEATURE_COLS, 'v3',
                                         BACKTEST_START, INITIAL_CASH, THRESHOLD)

    # Compute metrics
    print(f"\n[5/5] Computing metrics...")
    m_v2 = compute_metrics(res_v2['portfolio'], res_v2['prices'], INITIAL_CASH,
                           res_v2['predictions'], res_v2['actuals'],
                           res_v2['n_buy'], res_v2['n_sell'], res_v2['n_hold'])
    m_v3 = compute_metrics(res_v3['portfolio'], res_v3['prices'], INITIAL_CASH,
                           res_v3['predictions'], res_v3['actuals'],
                           res_v3['n_buy'], res_v3['n_sell'], res_v3['n_hold'])
    m_bnh = compute_bnh_metrics(res_v2['prices'], INITIAL_CASH)

    # Print comparison table
    print(f"\n{'=' * 70}")
    print(f"  SPY ±2% THRESHOLD BACKTEST: {BACKTEST_START} → {res_v2['dates'][-1].date()}")
    print(f"{'=' * 70}")
    print()
    print(f"  {'':25s} {'V2 Model':>12s}  {'V3 Advanced':>12s}  {'Buy & Hold':>12s}")
    print(f"  {'─' * 62}")
    print(f"  {'Final Value':25s} {'$'+str(m_v2['final_value']):>12s}  {'$'+str(m_v3['final_value']):>12s}  {'$'+str(m_bnh['final_value']):>12s}")
    print(f"  {'Total Return':25s} {m_v2['total_return']:>+11.1f}%  {m_v3['total_return']:>+11.1f}%  {m_bnh['total_return']:>+11.1f}%")
    print(f"  {'Annual Return':25s} {m_v2['annual_return']:>+11.1f}%  {m_v3['annual_return']:>+11.1f}%  {m_bnh['annual_return']:>+11.1f}%")
    print(f"  {'Sharpe Ratio':25s} {m_v2['sharpe']:>12.2f}  {m_v3['sharpe']:>12.2f}  {m_bnh['sharpe']:>12.2f}")
    print(f"  {'Max Drawdown':25s} {m_v2['max_drawdown']:>11.1f}%  {m_v3['max_drawdown']:>11.1f}%  {m_bnh['max_drawdown']:>11.1f}%")
    print(f"  {'Direction Accuracy':25s} {m_v2['direction_accuracy']:>11.1f}%  {m_v3['direction_accuracy']:>11.1f}%  {'—':>12s}")
    print(f"  {'─' * 62}")
    print(f"  {'BUY Signals':25s} {m_v2['n_buy']:>12d}  {m_v3['n_buy']:>12d}  {'—':>12s}")
    print(f"  {'SELL Signals':25s} {m_v2['n_sell']:>12d}  {m_v3['n_sell']:>12d}  {'—':>12s}")
    print(f"  {'HOLD Days':25s} {m_v2['n_hold']:>12d}  {m_v3['n_hold']:>12d}  {'—':>12s}")
    print(f"  {'Total Trades':25s} {m_v2['n_trades']:>12d}  {m_v3['n_trades']:>12d}  {'0':>12s}")
    print(f"  {'BUY Win Rate':25s} {m_v2['buy_win_rate']:>11.1f}%  {m_v3['buy_win_rate']:>11.1f}%  {'—':>12s}")
    print(f"  {'SELL Win Rate':25s} {m_v2['sell_win_rate']:>11.1f}%  {m_v3['sell_win_rate']:>11.1f}%  {'—':>12s}")
    print(f"  {'─' * 62}")
    print(f"  {'Test Days':25s} {m_v2['n_days']:>12d}  {m_v3['n_days']:>12d}  {m_bnh['n_days']:>12d}")

    # Chart
    min_len = min(len(res_v2['portfolio']), len(res_v3['portfolio']))
    dates_common = res_v2['dates'][:min_len]
    v2_eq = res_v2['portfolio'][:min_len]
    v3_eq = res_v3['portfolio'][:min_len]
    bnh_eq = m_bnh['equity'][:min_len]
    v3_sigs = res_v3['signals'][:min_len]

    plot_comparison(dates_common, v2_eq, v3_eq, bnh_eq, OUTPUT_CHART,
                    v3_signals=v3_sigs)

    print(f"\n{'=' * 70}")
    print(f"  DONE")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    run_comparison()
