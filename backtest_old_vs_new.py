"""
Quantfolio Backtest — Old (GitHub) vs New (Current) Model Comparison
=====================================================================
Compares the prior published versions against the updated versions:

  Old Lite:  RF(80%)+XGB(20%), 13 features, ±2% absolute threshold, retrain 20d
  New Lite:  RF(80%)+XGB(20%), 13 features, Z-score ±2.5σ threshold, retrain 63d

  Old Pro:   LGB+XGB+RF → Ridge meta + ±8% clip, 37 features, ±2% absolute, retrain 63d
  New Pro:   LGB+XGB+RF → inverse-MAE weighted (no clip), 22 features, Z-score ±2.5σ, retrain 63d

All compared against Buy & Hold.

Usage:
  python backtest_old_vs_new.py          # defaults to SPY
  python backtest_old_vs_new.py QQQ      # run for QQQ
  python backtest_old_vs_new.py AAPL     # run for any cached ticker
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
from sklearn.linear_model import Ridge
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
os.environ['LOKY_MAX_CPU_COUNT'] = '1'

# =============================================================================
# CONFIGURATION
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else 'SPY'
DATA_PATH = os.path.join(SCRIPT_DIR, "data_cache", f"{SYMBOL}.csv")
BACKTEST_START = '2015-01-02'
INITIAL_CASH = 10000
ZSCORE_LOOKBACK = 126
OUTPUT_CHART = os.path.join(SCRIPT_DIR, f"old_vs_new_{SYMBOL}.png")


# =============================================================================
# DATA
# =============================================================================
def load_data():
    df = pd.read_csv(DATA_PATH, index_col=0, parse_dates=True)
    df.sort_index(inplace=True)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    df.dropna(subset=['Close'], inplace=True)
    return df


# Safe RF predict
def rf_predict(rf, X):
    return np.mean([t.predict(X) for t in rf.estimators_], axis=0)


# =============================================================================
# FEATURES — V2 Lite (13 features, shared by old & new Lite)
# =============================================================================
V2_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'Dist_EMA50', 'Dist_EMA200',
    'RSI', 'BB_Position', 'BB_Width',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d',
    'SMA_Cross', 'EMA_Cross',
]

def engineer_v2(df):
    c = df['Close'].squeeze()
    s50 = SMAIndicator(c, 50).sma_indicator()
    s200 = SMAIndicator(c, 200).sma_indicator()
    e50 = EMAIndicator(c, 50).ema_indicator()
    e200 = EMAIndicator(c, 200).ema_indicator()
    rsi = RSIIndicator(c, 14).rsi()
    bb = BollingerBands(c, 20, 2)
    bh, bl = bb.bollinger_hband(), bb.bollinger_lband()
    df['Dist_SMA50'] = (c - s50) / s50; df['Dist_SMA200'] = (c - s200) / s200
    df['Dist_EMA50'] = (c - e50) / e50; df['Dist_EMA200'] = (c - e200) / e200
    df['RSI'] = rsi; df['BB_Position'] = (c - bl) / (bh - bl)
    df['BB_Width'] = (bh - bl) / c
    df['Return_1d'] = c.pct_change(1); df['Return_5d'] = c.pct_change(5)
    df['Return_20d'] = c.pct_change(20); df['RVol_20d'] = df['Return_1d'].rolling(20).std()
    df['SMA_Cross'] = (s50 > s200).astype(float)
    df['EMA_Cross'] = (e50 > e200).astype(float)
    df['Target_Return'] = c.pct_change(1).shift(-1)
    return df


# =============================================================================
# FEATURES — Old Pro (37 features)
# =============================================================================
OLD_PRO_COLS = [
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
    'RSI_Lag1', 'RSI_Lag2',
    'Return_1d_Lag1', 'Return_1d_Lag2',
    'BB_Position_Lag1', 'BB_Position_Lag2',
]

# =============================================================================
# FEATURES — New Pro (22 features)
# =============================================================================
NEW_PRO_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'RSI', 'BB_Position',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d', 'SMA_Cross',
    'Volume_Ratio_20d', 'OBV_Slope_10d', 'Volume_Zscore_20d',
    'ROC_10d', 'ROC_60d',
    'ATR_Norm', 'GK_Vol_20d',
    'Zscore_50d',
    'ADX_14', 'MACD_Hist_Norm',
    'RSI_Lag1', 'Return_1d_Lag1', 'BB_Position_Lag1',
]


def engineer_v3_full(df):
    """37 features (old Pro)."""
    c = df['Close'].squeeze(); h = df['High'].squeeze()
    l = df['Low'].squeeze(); o = df['Open'].squeeze()
    v = df['Volume'].squeeze().astype(float)
    s50 = SMAIndicator(c, 50).sma_indicator()
    s200 = SMAIndicator(c, 200).sma_indicator()
    e50 = EMAIndicator(c, 50).ema_indicator()
    e200 = EMAIndicator(c, 200).ema_indicator()
    rsi = RSIIndicator(c, 14).rsi()
    bb = BollingerBands(c, 20, 2)
    bh, bl = bb.bollinger_hband(), bb.bollinger_lband()
    df['Dist_SMA50'] = (c - s50) / s50; df['Dist_SMA200'] = (c - s200) / s200
    df['Dist_EMA50'] = (c - e50) / e50; df['Dist_EMA200'] = (c - e200) / e200
    df['RSI'] = rsi; df['BB_Position'] = (c - bl) / (bh - bl)
    df['BB_Width'] = (bh - bl) / c
    df['Return_1d'] = c.pct_change(1); df['Return_5d'] = c.pct_change(5)
    df['Return_20d'] = c.pct_change(20); df['RVol_20d'] = df['Return_1d'].rolling(20).std()
    df['SMA_Cross'] = (s50 > s200).astype(float)
    df['EMA_Cross'] = (e50 > e200).astype(float)
    vs20 = v.rolling(20).mean()
    df['Volume_Ratio_20d'] = v / vs20
    obv = OnBalanceVolumeIndicator(c, v).on_balance_volume()
    obv_s = obv.rolling(10).apply(lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 10 else 0, raw=True)
    df['OBV_Slope_10d'] = obv_s / (v.rolling(10).mean() + 1e-10)
    df['Volume_Price_Div'] = (np.sign(c.pct_change(5)) != np.sign(v.pct_change(5))).astype(float)
    df['Volume_Zscore_20d'] = (v - vs20) / (v.rolling(20).std() + 1e-10)
    df['ROC_3d'] = ROCIndicator(c, 3).roc(); df['ROC_10d'] = ROCIndicator(c, 10).roc()
    df['ROC_20d'] = ROCIndicator(c, 20).roc(); df['ROC_60d'] = ROCIndicator(c, 60).roc()
    atr = AverageTrueRange(h, l, c, 14).average_true_range(); df['ATR_Norm'] = atr / c
    lhl = np.log(h / l) ** 2; lco = np.log(c / o) ** 2
    df['GK_Vol_20d'] = (0.5 * lhl - (2 * np.log(2) - 1) * lco).rolling(20).mean()
    df['Intraday_Range'] = (h - l) / c
    s20 = SMAIndicator(c, 20).sma_indicator()
    df['Zscore_20d'] = (c - s20) / (c.rolling(20).std() + 1e-10)
    df['Zscore_50d'] = (c - s50) / (c.rolling(50).std() + 1e-10)
    df['ADX_14'] = ADXIndicator(h, l, c, 14).adx()
    macd = MACD(c, 26, 12, 9); df['MACD_Hist_Norm'] = macd.macd_diff() / c
    df['Day_of_Week'] = df.index.dayofweek.astype(float)
    df['Month'] = df.index.month.astype(float)
    qe = pd.Series(df.index, index=df.index).apply(lambda d: 1.0 if d.month in [3,6,9,12] and d.day >= 25 else 0.0)
    df['Quarter_End'] = qe.values
    df['RSI_Lag1'] = rsi.shift(1); df['RSI_Lag2'] = rsi.shift(2)
    df['Return_1d_Lag1'] = df['Return_1d'].shift(1); df['Return_1d_Lag2'] = df['Return_1d'].shift(2)
    df['BB_Position_Lag1'] = df['BB_Position'].shift(1); df['BB_Position_Lag2'] = df['BB_Position'].shift(2)
    df['Target_Return'] = c.pct_change(1).shift(-1)
    return df


# =============================================================================
# MODEL BUILDERS
# =============================================================================
def train_rf_v2(X, y):
    m = RandomForestRegressor(n_estimators=100, max_depth=6, min_samples_leaf=10,
                              max_features='sqrt', random_state=42, n_jobs=1)
    m.fit(X, y); return m

def train_xgb_v2(X, y):
    m = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=100, max_depth=4,
                          learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                          reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1)
    m.fit(X, y, verbose=False); return m

def predict_lite(models, X):
    return np.clip(0.8 * rf_predict(models[0], X) + 0.2 * models[1].predict(X), -0.08, 0.08)

def train_lgbm(Xt, yt, Xv, yv):
    m = lgb.LGBMRegressor(n_estimators=500, max_depth=5, learning_rate=0.05,
                           subsample=0.7, colsample_bytree=0.7, reg_alpha=0.3, reg_lambda=1.5,
                           min_child_samples=20, random_state=42, verbose=-1, n_jobs=1)
    m.fit(Xt, yt, eval_set=[(Xv, yv)], callbacks=[lgb.early_stopping(30, verbose=False)])
    return m

def train_xgb_v3(Xt, yt, Xv, yv):
    m = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=500, max_depth=4,
                          learning_rate=0.05, subsample=0.7, colsample_bytree=0.7,
                          reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1,
                          early_stopping_rounds=30)
    m.fit(Xt, yt, eval_set=[(Xv, yv)], verbose=False); return m

def train_rf_v3(X, y):
    m = RandomForestRegressor(n_estimators=150, max_depth=8, min_samples_leaf=15,
                              max_features=0.5, random_state=42, n_jobs=1)
    m.fit(X, y); return m


# --- OLD Pro: Ridge meta + clip ---
def build_old_pro(X_train, y_train, X_val, y_val):
    tscv = TimeSeriesSplit(n_splits=3)
    oof = np.zeros((len(X_train), 3))
    for _, (ti, vi) in enumerate(tscv.split(X_train)):
        Xft, yft = X_train[ti], y_train[ti]
        Xfv, yfv = X_train[vi], y_train[vi]
        oof[vi, 0] = train_lgbm(Xft, yft, Xfv, yfv).predict(Xfv)
        oof[vi, 1] = train_xgb_v3(Xft, yft, Xfv, yfv).predict(Xfv)
        oof[vi, 2] = rf_predict(train_rf_v3(Xft, yft), Xfv)
    mask = np.any(oof != 0, axis=1)
    meta = Ridge(alpha=0.01)
    meta.fit(oof[mask], y_train[mask])
    fl = train_lgbm(X_train, y_train, X_val, y_val)
    fx = train_xgb_v3(X_train, y_train, X_val, y_val)
    fr = train_rf_v3(X_train, y_train)
    return {'lgbm': fl, 'xgb': fx, 'rf': fr, 'meta': meta}

def predict_old_pro(ens, X):
    st = np.column_stack([ens['lgbm'].predict(X), ens['xgb'].predict(X), rf_predict(ens['rf'], X)])
    return np.clip(ens['meta'].predict(st), -0.08, 0.08)


# --- NEW Pro: inverse-MAE weighted, no clip ---
def build_new_pro(X_train, y_train, X_val, y_val):
    tscv = TimeSeriesSplit(n_splits=3)
    oof = np.zeros((len(X_train), 3))
    for _, (ti, vi) in enumerate(tscv.split(X_train)):
        Xft, yft = X_train[ti], y_train[ti]
        Xfv, yfv = X_train[vi], y_train[vi]
        oof[vi, 0] = train_lgbm(Xft, yft, Xfv, yfv).predict(Xfv)
        oof[vi, 1] = train_xgb_v3(Xft, yft, Xfv, yfv).predict(Xfv)
        oof[vi, 2] = rf_predict(train_rf_v3(Xft, yft), Xfv)
    mask = np.any(oof != 0, axis=1)
    mae = np.array([np.mean(np.abs(oof[mask, j] - y_train[mask])) for j in range(3)])
    inv = 1.0 / (mae + 1e-10); w = inv / inv.sum()
    fl = train_lgbm(X_train, y_train, X_val, y_val)
    fx = train_xgb_v3(X_train, y_train, X_val, y_val)
    fr = train_rf_v3(X_train, y_train)
    return {'lgbm': fl, 'xgb': fx, 'rf': fr, 'weights': w}

def predict_new_pro(ens, X):
    w = ens['weights']
    return w[0]*ens['lgbm'].predict(X) + w[1]*ens['xgb'].predict(X) + w[2]*rf_predict(ens['rf'], X)


# =============================================================================
# WALK-FORWARD BACKTEST
# =============================================================================
def run_backtest(df, feature_cols, model_name, build_fn, predict_fn,
                 retrain_freq, threshold, use_zscore, rescale):
    """
    Unified backtest engine.
    use_zscore=False: old absolute threshold (±2%)
    use_zscore=True:  new Z-score threshold (±2.5σ)
    rescale=True:     old rescaling (std matching on validation)
    rescale=False:    new (no rescaling needed)
    """
    df_clean = df.dropna(subset=['Target_Return'] + feature_cols).copy()
    bt_mask = df_clean.index >= pd.Timestamp(BACKTEST_START)
    if bt_mask.sum() == 0:
        raise ValueError(f"No data after {BACKTEST_START}")
    bt_start_idx = np.argmax(bt_mask)
    all_X = df_clean[feature_cols].values
    all_y = df_clean['Target_Return'].values
    all_prices = df_clean['Close'].values.ravel()
    all_dates = df_clean.index

    cash, shares = INITIAL_CASH, 0.0
    portfolio, signals = [], []
    model = None; scaler = StandardScaler()
    retrain_counter = 0; pred_history = []; sf = 1.0
    retrain_times = []
    total_test = len(all_X) - bt_start_idx - 1
    n_retrains = total_test // retrain_freq + 1

    sig_type = "Z-score" if use_zscore else "Absolute"
    print(f"  [{model_name}] {total_test} days, ~{n_retrains} retrains, "
          f"{sig_type} ±{threshold}", flush=True)

    for i in range(bt_start_idx, len(all_X) - 1):
        if model is None or retrain_counter >= retrain_freq:
            t0 = time.time()
            train_end = i
            X_all = all_X[:train_end]; y_all = all_y[:train_end]
            vs = int(len(X_all) * 0.85)
            scaler.fit(X_all)
            X_tr_s = scaler.transform(X_all[:vs])
            X_val_s = scaler.transform(X_all[vs:])
            y_tr, y_val = y_all[:vs], y_all[vs:]

            if build_fn == 'lite':
                X_full_s = scaler.transform(X_all)
                model = (train_rf_v2(X_full_s, y_all), train_xgb_v2(X_full_s, y_all))
                pf = predict_lite
            else:
                model = build_fn(X_tr_s, y_tr, X_val_s, y_val)
                pf = predict_fn

            # Rescaling (old method: match validation std)
            if rescale:
                cp = pf(model, X_val_s)
                ps = np.std(cp)
                sf = np.std(y_val) / ps if ps > 1e-10 else 1.0
            else:
                sf = 1.0

            # Seed Z-score history
            if use_zscore and not pred_history:
                seed = pf(model, X_val_s) * sf
                pred_history = list(seed[-ZSCORE_LOOKBACK:])

            elapsed = time.time() - t0
            retrain_times.append(elapsed)
            done = len(retrain_times)
            print(f"    retrain {done}/{n_retrains} ({done*100//n_retrains}%) — {elapsed:.1f}s",
                  flush=True)
            retrain_counter = 0

        X_today = scaler.transform(all_X[i:i+1])
        raw_pred = pf(model, X_today)[0] * sf
        price = float(all_prices[i])

        if use_zscore:
            # Z-score signal
            pred_history.append(raw_pred)
            if len(pred_history) > ZSCORE_LOOKBACK:
                pred_history = pred_history[-ZSCORE_LOOKBACK:]
            if len(pred_history) >= 20:
                hist = np.array(pred_history[:-1])
                mu, sigma = np.mean(hist), np.std(hist)
                signal_val = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
            else:
                signal_val = 0.0
        else:
            # Absolute signal (old: raw_pred is already rescaled percentage)
            signal_val = raw_pred

        if signal_val >= threshold and cash > 0:
            shares = cash / price; cash = 0; signals.append('BUY')
        elif signal_val <= -threshold and shares > 0:
            cash = shares * price; shares = 0; signals.append('SELL')
        else:
            signals.append('HOLD')

        portfolio.append(cash + shares * price)
        retrain_counter += 1

    n_buy, n_sell = signals.count('BUY'), signals.count('SELL')
    avg_t = np.mean(retrain_times) if retrain_times else 0
    print(f"  [{model_name}] {n_buy} BUY, {n_sell} SELL, {n_buy+n_sell} trades | "
          f"total train: {sum(retrain_times):.0f}s")

    return {
        'dates': all_dates[bt_start_idx:bt_start_idx + len(portfolio)],
        'portfolio': np.array(portfolio),
        'signals': signals,
        'n_buy': n_buy, 'n_sell': n_sell,
        'total_time': round(sum(retrain_times), 0),
    }


# =============================================================================
# METRICS & BOOTSTRAP
# =============================================================================
def compute_metrics(portfolio):
    total_ret = (portfolio[-1] / INITIAL_CASH - 1) * 100
    n_years = len(portfolio) / 252
    annual_ret = ((portfolio[-1] / INITIAL_CASH) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0
    daily_rets = np.diff(portfolio) / portfolio[:-1]
    sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252)) if np.std(daily_rets) > 0 else 0
    peak = np.maximum.accumulate(portfolio)
    max_dd = float(((portfolio - peak) / peak).min()) * 100
    return {
        'final': round(portfolio[-1], 2),
        'total_ret': round(total_ret, 1),
        'annual_ret': round(annual_ret, 1),
        'sharpe': round(sharpe, 2),
        'max_dd': round(max_dd, 1),
    }

def bootstrap_sharpe(portfolio, n_boot=10000, seed=42):
    dr = np.diff(portfolio) / portfolio[:-1]
    if len(dr) < 30 or np.std(dr) == 0: return 0, 0, 0
    obs = float(np.mean(dr) / np.std(dr) * np.sqrt(252))
    rng = np.random.RandomState(seed)
    boots = np.empty(n_boot)
    for b in range(n_boot):
        s = rng.choice(dr, size=len(dr), replace=True)
        std = np.std(s)
        boots[b] = np.mean(s) / std * np.sqrt(252) if std > 0 else 0
    return obs, np.percentile(boots, 2.5), np.percentile(boots, 97.5)


# =============================================================================
# CHART
# =============================================================================
def plot_results(dates, results, bnh_eq, output):
    colors = {'Old Lite': '#90CAF9', 'New Lite': '#2196F3',
              'Old Pro': '#FFAB91', 'New Pro': '#FF5722'}
    fig, axes = plt.subplots(2, 1, figsize=(16, 10), sharex=True,
                              gridspec_kw={'height_ratios': [3, 1]})
    ax1, ax2 = axes
    dp = pd.to_datetime(dates)

    for name, res in results.items():
        ax1.plot(dp, res['portfolio'][:len(dp)], label=name, lw=1.5, color=colors.get(name, 'gray'))
    ax1.plot(dp, bnh_eq[:len(dp)], label='Buy & Hold', lw=1.0, color='#9E9E9E', ls='--')
    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title(f'{SYMBOL} Backtest: Old (GitHub) vs New (Current) Models')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    for name, res in results.items():
        p = res['portfolio'][:len(dp)]
        peak = np.maximum.accumulate(p)
        dd = (p - peak) / peak * 100
        ax2.plot(dp, dd, lw=0.8, color=colors.get(name, 'gray'), label=name)
    bpk = np.maximum.accumulate(bnh_eq[:len(dp)])
    ax2.plot(dp, (bnh_eq[:len(dp)] - bpk) / bpk * 100, lw=0.8, color='#9E9E9E', ls='--', label='B&H')
    ax2.set_ylabel('Drawdown (%)')
    ax2.legend(loc='lower left', fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.set_xlabel('Date')
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches='tight')
    print(f"\n  Chart saved: {output}")


# =============================================================================
# MAIN
# =============================================================================
def run():
    t_start = time.time()
    print("=" * 80)
    print(f"  QUANTFOLIO — OLD vs NEW MODEL COMPARISON ({SYMBOL})")
    print("=" * 80)
    print(f"  Old Lite: RF+XGB, 13 feat, ±2% absolute, retrain 20d")
    print(f"  New Lite: RF+XGB, 13 feat, Z ±2.5σ, retrain 63d")
    print(f"  Old Pro:  LGB+XGB+RF → Ridge+clip, 37 feat, ±2% absolute, retrain 63d")
    print(f"  New Pro:  LGB+XGB+RF → inv-MAE, 22 feat, Z ±2.5σ, retrain 63d")

    print(f"\n[1/7] Loading {SYMBOL} data...")
    df_raw = load_data()
    print(f"  {len(df_raw)} rows: {df_raw.index[0].date()} to {df_raw.index[-1].date()}")

    print(f"\n[2/7] Engineering features...")
    df_v2 = engineer_v2(df_raw.copy())
    df_v3_full = engineer_v3_full(df_raw.copy())  # 37 features for old Pro
    # New Pro uses subset of v3_full columns (22 of the 37 are present)

    print(f"\n[3/7] Old Lite backtest...")
    res_old_lite = run_backtest(df_v2, V2_COLS, 'Old Lite', 'lite', predict_lite,
                                retrain_freq=20, threshold=0.02,
                                use_zscore=False, rescale=True)

    print(f"\n[4/7] New Lite backtest...")
    res_new_lite = run_backtest(df_v2, V2_COLS, 'New Lite', 'lite', predict_lite,
                                retrain_freq=63, threshold=2.5,
                                use_zscore=True, rescale=False)

    print(f"\n[5/7] Old Pro backtest...")
    res_old_pro = run_backtest(df_v3_full, OLD_PRO_COLS, 'Old Pro',
                               build_old_pro, predict_old_pro,
                               retrain_freq=63, threshold=0.02,
                               use_zscore=False, rescale=True)

    print(f"\n[6/7] New Pro backtest...")
    res_new_pro = run_backtest(df_v3_full, NEW_PRO_COLS, 'New Pro',
                               build_new_pro, predict_new_pro,
                               retrain_freq=63, threshold=2.5,
                               use_zscore=True, rescale=False)

    # Buy & Hold
    df_bnh = df_v2.dropna(subset=['Target_Return'] + V2_COLS).copy()
    bt_prices = df_bnh.loc[df_bnh.index >= pd.Timestamp(BACKTEST_START), 'Close'].values.ravel()
    bnh_eq = INITIAL_CASH * (bt_prices / bt_prices[0])

    print(f"\n[7/7] Metrics & bootstrap CIs...")
    all_results = {
        'Old Lite': res_old_lite, 'New Lite': res_new_lite,
        'Old Pro': res_old_pro, 'New Pro': res_new_pro,
    }

    metrics = {}
    sharpe_cis = {}
    for name, res in all_results.items():
        metrics[name] = compute_metrics(res['portfolio'])
        sharpe_cis[name] = bootstrap_sharpe(res['portfolio'])
    metrics['B&H'] = compute_metrics(bnh_eq)
    sharpe_cis['B&H'] = bootstrap_sharpe(bnh_eq)

    total_time = time.time() - t_start

    # Print table
    names = ['Old Lite', 'New Lite', 'Old Pro', 'New Pro', 'B&H']
    print(f"\n{'='*95}")
    print(f"  {SYMBOL} BACKTEST: {BACKTEST_START} -> {res_old_lite['dates'][-1].date()}")
    print(f"{'='*95}\n")

    hdr = f"  {'':18s}" + "".join(f"{n:>14s}" for n in names)
    print(hdr)
    print(f"  {'─'*88}")

    print(f"  {'Final Value':18s}" + "".join(f"{'$'+str(metrics[n]['final']):>14s}" for n in names))
    print(f"  {'Total Return':18s}" + "".join(f"{metrics[n]['total_ret']:>+13.1f}%" for n in names))
    print(f"  {'Annual Return':18s}" + "".join(f"{metrics[n]['annual_ret']:>+13.1f}%" for n in names))
    print(f"  {'Sharpe Ratio':18s}" + "".join(f"{metrics[n]['sharpe']:>14.2f}" for n in names))
    ci_strs = [f"[{sharpe_cis[n][1]:+.2f},{sharpe_cis[n][2]:+.2f}]" for n in names]
    print(f"  {'  95% CI':18s}" + "".join(f"{s:>14s}" for s in ci_strs))
    print(f"  {'Max Drawdown':18s}" + "".join(f"{metrics[n]['max_dd']:>13.1f}%" for n in names))
    print(f"  {'─'*88}")

    for n in ['Old Lite', 'New Lite', 'Old Pro', 'New Pro']:
        res = all_results[n]
        trades = res['n_buy'] + res['n_sell']
        print(f"  {n+' Trades':18s} {trades:>14d}")

    print(f"  {'─'*88}")
    print(f"\n  IMPROVEMENTS:")
    lite_imp = metrics['New Lite']['sharpe'] - metrics['Old Lite']['sharpe']
    pro_imp = metrics['New Pro']['sharpe'] - metrics['Old Pro']['sharpe']
    print(f"  Lite: Sharpe {metrics['Old Lite']['sharpe']:.2f} → {metrics['New Lite']['sharpe']:.2f} ({lite_imp:+.2f})")
    print(f"  Pro:  Sharpe {metrics['Old Pro']['sharpe']:.2f} → {metrics['New Pro']['sharpe']:.2f} ({pro_imp:+.2f})")
    print(f"\n  Total wall time: {total_time:.0f}s ({total_time/60:.1f}min)")

    # Chart
    min_len = min(len(res['portfolio']) for res in all_results.values())
    min_len = min(min_len, len(bnh_eq))
    plot_results(res_old_lite['dates'][:min_len], all_results, bnh_eq, OUTPUT_CHART)

    print(f"\n{'='*80}")
    print(f"  DONE")
    print(f"{'='*80}")


if __name__ == '__main__':
    run()
    sys.stdout.flush()
