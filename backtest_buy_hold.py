"""
Quantfolio — Buy-Only vs Full Signal Backtest
===============================================
Tests whether SELL signals help or hurt by comparing:

  Buy & Hold   — buy on day 1, hold forever
  Pro Buy-Only — buy on first Pro BUY signal (Z >= 2.5), never sell
  Pro Full     — BUY/SELL with Z-score +/-2.5 (current strategy)
  Lite Buy-Only — buy on first Lite BUY signal, never sell
  Lite Full    — BUY/SELL with Z-score +/-2.5

Usage:
  python backtest_buy_hold.py SPY
  python backtest_buy_hold.py SPY QQQ AAPL NVDA MSFT
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
BACKTEST_START = '2015-01-02'
INITIAL_CASH = 10000
ZSCORE_LOOKBACK = 126
THRESHOLD = 2.5
RETRAIN_FREQ = 63


# =============================================================================
# DATA & HELPERS
# =============================================================================
def load_data(symbol):
    path = os.path.join(SCRIPT_DIR, "data_cache", f"{symbol}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No cached data for {symbol}: {path}")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.sort_index(inplace=True)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    df.dropna(subset=['Close'], inplace=True)
    return df


def rf_predict(rf, X):
    return np.mean([t.predict(X) for t in rf.estimators_], axis=0)


# =============================================================================
# FEATURES — Lite (13 features)
# =============================================================================
LITE_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'Dist_EMA50', 'Dist_EMA200',
    'RSI', 'BB_Position', 'BB_Width',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d',
    'SMA_Cross', 'EMA_Cross',
]

def engineer_lite(df):
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
# FEATURES — Pro (22 features)
# =============================================================================
PRO_COLS = [
    'Dist_SMA50', 'Dist_SMA200', 'RSI', 'BB_Position',
    'Return_1d', 'Return_5d', 'Return_20d', 'RVol_20d', 'SMA_Cross',
    'Volume_Ratio_20d', 'OBV_Slope_10d', 'Volume_Zscore_20d',
    'ROC_10d', 'ROC_60d',
    'ATR_Norm', 'GK_Vol_20d',
    'Zscore_50d',
    'ADX_14', 'MACD_Hist_Norm',
    'RSI_Lag1', 'Return_1d_Lag1', 'BB_Position_Lag1',
]

def engineer_pro(df):
    c = df['Close'].squeeze(); h = df['High'].squeeze()
    l = df['Low'].squeeze(); o = df['Open'].squeeze()
    v = df['Volume'].squeeze().astype(float)
    s50 = SMAIndicator(c, 50).sma_indicator()
    s200 = SMAIndicator(c, 200).sma_indicator()
    rsi = RSIIndicator(c, 14).rsi()
    bb = BollingerBands(c, 20, 2)
    bh, bl = bb.bollinger_hband(), bb.bollinger_lband()
    df['Dist_SMA50'] = (c - s50) / s50; df['Dist_SMA200'] = (c - s200) / s200
    df['RSI'] = rsi; df['BB_Position'] = (c - bl) / (bh - bl)
    df['Return_1d'] = c.pct_change(1); df['Return_5d'] = c.pct_change(5)
    df['Return_20d'] = c.pct_change(20); df['RVol_20d'] = df['Return_1d'].rolling(20).std()
    df['SMA_Cross'] = (s50 > s200).astype(float)
    vs20 = v.rolling(20).mean()
    df['Volume_Ratio_20d'] = v / vs20
    obv = OnBalanceVolumeIndicator(c, v).on_balance_volume()
    obv_s = obv.rolling(10).apply(lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 10 else 0, raw=True)
    df['OBV_Slope_10d'] = obv_s / (v.rolling(10).mean() + 1e-10)
    df['Volume_Zscore_20d'] = (v - vs20) / (v.rolling(20).std() + 1e-10)
    df['ROC_10d'] = ROCIndicator(c, 10).roc(); df['ROC_60d'] = ROCIndicator(c, 60).roc()
    atr = AverageTrueRange(h, l, c, 14).average_true_range(); df['ATR_Norm'] = atr / c
    lhl = np.log(h / l) ** 2; lco = np.log(c / o) ** 2
    df['GK_Vol_20d'] = (0.5 * lhl - (2 * np.log(2) - 1) * lco).rolling(20).mean()
    df['Zscore_50d'] = (c - s50) / (c.rolling(50).std() + 1e-10)
    df['ADX_14'] = ADXIndicator(h, l, c, 14).adx()
    macd = MACD(c, 26, 12, 9); df['MACD_Hist_Norm'] = macd.macd_diff() / c
    df['RSI_Lag1'] = rsi.shift(1)
    df['Return_1d_Lag1'] = df['Return_1d'].shift(1)
    df['BB_Position_Lag1'] = df['BB_Position'].shift(1)
    df['Target_Return'] = c.pct_change(1).shift(-1)
    return df


# =============================================================================
# MODEL BUILDERS
# =============================================================================
def train_lite(X, y):
    rf = RandomForestRegressor(n_estimators=100, max_depth=6, min_samples_leaf=10,
                               max_features='sqrt', random_state=42, n_jobs=1)
    xg = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=100, max_depth=4,
                           learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                           reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1)
    rf.fit(X, y); xg.fit(X, y, verbose=False)
    return {'rf': rf, 'xgb': xg}

def predict_lite(model, X):
    return np.clip(0.8 * rf_predict(model['rf'], X) + 0.2 * model['xgb'].predict(X), -0.08, 0.08)


def build_pro(X_train, y_train, X_val, y_val):
    tscv = TimeSeriesSplit(n_splits=3)
    oof = np.zeros((len(X_train), 3))
    for _, (ti, vi) in enumerate(tscv.split(X_train)):
        Xft, yft = X_train[ti], y_train[ti]
        Xfv, yfv = X_train[vi], y_train[vi]
        fl = lgb.LGBMRegressor(n_estimators=500, max_depth=5, learning_rate=0.05,
                                subsample=0.7, colsample_bytree=0.7, reg_alpha=0.3, reg_lambda=1.5,
                                min_child_samples=20, random_state=42, verbose=-1, n_jobs=1)
        fl.fit(Xft, yft, eval_set=[(Xfv, yfv)], callbacks=[lgb.early_stopping(30, verbose=False)])
        oof[vi, 0] = fl.predict(Xfv)

        fx = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=500, max_depth=4,
                               learning_rate=0.05, subsample=0.7, colsample_bytree=0.7,
                               reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1,
                               early_stopping_rounds=30)
        fx.fit(Xft, yft, eval_set=[(Xfv, yfv)], verbose=False)
        oof[vi, 1] = fx.predict(Xfv)

        fr = RandomForestRegressor(n_estimators=150, max_depth=8, min_samples_leaf=15,
                                    max_features=0.5, random_state=42, n_jobs=1)
        fr.fit(Xft, yft)
        oof[vi, 2] = rf_predict(fr, Xfv)

    mask = np.any(oof != 0, axis=1)
    mae = np.array([np.mean(np.abs(oof[mask, j] - y_train[mask])) for j in range(3)])
    inv = 1.0 / (mae + 1e-10); w = inv / inv.sum()

    # Final models on full training set
    fl = lgb.LGBMRegressor(n_estimators=500, max_depth=5, learning_rate=0.05,
                            subsample=0.7, colsample_bytree=0.7, reg_alpha=0.3, reg_lambda=1.5,
                            min_child_samples=20, random_state=42, verbose=-1, n_jobs=1)
    fl.fit(X_train, y_train, eval_set=[(X_val, y_val)], callbacks=[lgb.early_stopping(30, verbose=False)])

    fx = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=500, max_depth=4,
                           learning_rate=0.05, subsample=0.7, colsample_bytree=0.7,
                           reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=1,
                           early_stopping_rounds=30)
    fx.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

    fr = RandomForestRegressor(n_estimators=150, max_depth=8, min_samples_leaf=15,
                                max_features=0.5, random_state=42, n_jobs=1)
    fr.fit(X_train, y_train)

    return {'lgbm': fl, 'xgb': fx, 'rf': fr, 'weights': w}

def predict_pro(ens, X):
    w = ens['weights']
    return w[0]*ens['lgbm'].predict(X) + w[1]*ens['xgb'].predict(X) + w[2]*rf_predict(ens['rf'], X)


# =============================================================================
# WALK-FORWARD ENGINE — runs all strategies in one pass
# =============================================================================
def run_backtest(df, feature_cols, model_type, symbol):
    """
    Single walk-forward pass that tracks THREE strategies simultaneously:
      1. Full Signal  — BUY when Z >= 2.5, SELL when Z <= -2.5
      2. Buy-Only     — BUY when Z >= 2.5, never sell (hold forever)
      3. Buy & Hold   — buy on day 1
    Trains the model once per retrain, evaluates all strategies on same predictions.
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

    # Strategy states
    full_cash, full_shares = INITIAL_CASH, 0.0
    bo_cash, bo_shares = INITIAL_CASH, 0.0     # buy-only
    bo_bought = False                            # once True, never sell

    full_portfolio, bo_portfolio = [], []
    full_signals, bo_signals = [], []

    model = None; scaler = StandardScaler()
    retrain_counter = 0; pred_history = []
    retrain_times = []
    total_test = len(all_X) - bt_start_idx - 1
    n_retrains = total_test // RETRAIN_FREQ + 1

    print(f"  [{model_type}] {total_test} days, ~{n_retrains} retrains", flush=True)

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

            if model_type == 'Lite':
                X_full_s = scaler.transform(X_all)
                model = train_lite(X_full_s, y_all)
                pf = predict_lite
            else:
                model = build_pro(X_tr_s, y_tr, X_val_s, y_val)
                pf = predict_pro

            # Seed Z-score history
            if not pred_history:
                seed = pf(model, X_val_s)
                pred_history = list(seed[-ZSCORE_LOOKBACK:])

            elapsed = time.time() - t0
            retrain_times.append(elapsed)
            done = len(retrain_times)
            print(f"    retrain {done}/{n_retrains} ({done*100//n_retrains}%) — {elapsed:.1f}s",
                  flush=True)
            retrain_counter = 0

        X_today = scaler.transform(all_X[i:i+1])
        raw_pred = pf(model, X_today)[0]
        price = float(all_prices[i])

        # Z-score signal
        pred_history.append(raw_pred)
        if len(pred_history) > ZSCORE_LOOKBACK:
            pred_history = pred_history[-ZSCORE_LOOKBACK:]
        if len(pred_history) >= 20:
            hist = np.array(pred_history[:-1])
            mu, sigma = np.mean(hist), np.std(hist)
            z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
        else:
            z = 0.0

        # --- Full Signal strategy ---
        if z >= THRESHOLD and full_cash > 0:
            full_shares = full_cash / price; full_cash = 0; full_signals.append('BUY')
        elif z <= -THRESHOLD and full_shares > 0:
            full_cash = full_shares * price; full_shares = 0; full_signals.append('SELL')
        else:
            full_signals.append('HOLD')
        full_portfolio.append(full_cash + full_shares * price)

        # --- Buy-Only strategy ---
        if not bo_bought and z >= THRESHOLD and bo_cash > 0:
            bo_shares = bo_cash / price; bo_cash = 0; bo_bought = True
            bo_signals.append('BUY')
        else:
            bo_signals.append('HOLD')
        bo_portfolio.append(bo_cash + bo_shares * price)

        retrain_counter += 1

    # Buy & Hold equity
    bt_prices = all_prices[bt_start_idx:bt_start_idx + len(full_portfolio)]
    bnh_eq = INITIAL_CASH * (bt_prices / bt_prices[0])

    dates = all_dates[bt_start_idx:bt_start_idx + len(full_portfolio)]

    full_buys = full_signals.count('BUY')
    full_sells = full_signals.count('SELL')
    bo_buys = bo_signals.count('BUY')

    # Find buy-only entry date
    bo_entry = 'Never'
    for idx, sig in enumerate(bo_signals):
        if sig == 'BUY':
            bo_entry = str(dates[idx].date())
            break

    print(f"  [{model_type} Full]     {full_buys} BUY, {full_sells} SELL")
    print(f"  [{model_type} Buy-Only] {bo_buys} BUY — entry: {bo_entry}")
    print(f"  Total train time: {sum(retrain_times):.0f}s")

    return {
        'dates': dates,
        'bnh': bnh_eq,
        'full': np.array(full_portfolio),
        'buy_only': np.array(bo_portfolio),
        'full_buys': full_buys, 'full_sells': full_sells,
        'bo_entry': bo_entry,
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
    rng = np.random.RandomState(seed)
    boots = np.empty(n_boot)
    for b in range(n_boot):
        s = rng.choice(dr, size=len(dr), replace=True)
        std = np.std(s)
        boots[b] = np.mean(s) / std * np.sqrt(252) if std > 0 else 0
    return np.percentile(boots, 2.5), np.percentile(boots, 97.5)


# =============================================================================
# CHART
# =============================================================================
def plot_symbol(symbol, dates, results):
    colors = {
        'Pro Full': '#FF5722', 'Pro Buy-Only': '#FF9800',
        'Lite Full': '#2196F3', 'Lite Buy-Only': '#64B5F6',
        'B&H': '#9E9E9E',
    }
    fig, axes = plt.subplots(2, 1, figsize=(16, 10), sharex=True,
                              gridspec_kw={'height_ratios': [3, 1]})
    ax1, ax2 = axes
    dp = pd.to_datetime(dates)

    for name, eq in results.items():
        ls = '--' if name == 'B&H' else '-'
        ax1.plot(dp, eq[:len(dp)], label=name, lw=1.5, color=colors.get(name, 'gray'), ls=ls)
    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title(f'{symbol}: Buy-Only vs Full Signal Backtest')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    for name, eq in results.items():
        p = eq[:len(dp)]
        peak = np.maximum.accumulate(p)
        dd = (p - peak) / peak * 100
        ls = '--' if name == 'B&H' else '-'
        ax2.plot(dp, dd, lw=0.8, color=colors.get(name, 'gray'), label=name, ls=ls)
    ax2.set_ylabel('Drawdown (%)')
    ax2.legend(loc='lower left', fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.set_xlabel('Date')
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    plt.tight_layout()
    out = os.path.join(SCRIPT_DIR, f"buy_hold_{symbol}.png")
    plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"  Chart saved: {out}")


# =============================================================================
# PRINT TABLE
# =============================================================================
def print_table(symbol, dates, results_dict, entries):
    names = ['Pro Full', 'Pro Buy-Only', 'Lite Full', 'Lite Buy-Only', 'B&H']
    metrics = {n: compute_metrics(results_dict[n]) for n in names}
    cis = {n: bootstrap_sharpe(results_dict[n]) for n in names}

    print(f"\n{'='*100}")
    print(f"  {symbol} BACKTEST: {BACKTEST_START} -> {dates[-1].date()}")
    print(f"{'='*100}\n")

    hdr = f"  {'':18s}" + "".join(f"{n:>16s}" for n in names)
    print(hdr)
    print(f"  {'─'*98}")
    print(f"  {'Final Value':18s}" + "".join(f"{'$'+str(metrics[n]['final']):>16s}" for n in names))
    print(f"  {'Total Return':18s}" + "".join(f"{metrics[n]['total_ret']:>+15.1f}%" for n in names))
    print(f"  {'Annual Return':18s}" + "".join(f"{metrics[n]['annual_ret']:>+15.1f}%" for n in names))
    print(f"  {'Sharpe Ratio':18s}" + "".join(f"{metrics[n]['sharpe']:>16.2f}" for n in names))
    ci_strs = [f"[{cis[n][0]:+.2f},{cis[n][1]:+.2f}]" for n in names]
    print(f"  {'  95% CI':18s}" + "".join(f"{s:>16s}" for s in ci_strs))
    print(f"  {'Max Drawdown':18s}" + "".join(f"{metrics[n]['max_dd']:>15.1f}%" for n in names))
    print(f"  {'─'*98}")
    print(f"  {'Pro Entry Date':18s} {'':>16s} {entries['Pro']:>16s}")
    print(f"  {'Lite Entry Date':18s} {'':>16s} {'':>16s} {'':>16s} {entries['Lite']:>16s}")
    print(f"  {'─'*98}")

    # Buy-only vs Full comparison
    print(f"\n  SELL SIGNAL VALUE:")
    for model in ['Pro', 'Lite']:
        full_s = metrics[f'{model} Full']['sharpe']
        bo_s = metrics[f'{model} Buy-Only']['sharpe']
        diff = full_s - bo_s
        verdict = "SELL helps" if diff > 0.05 else "SELL hurts" if diff < -0.05 else "~neutral"
        print(f"  {model}: Full {full_s:.2f} vs Buy-Only {bo_s:.2f} ({diff:+.2f}) — {verdict}")

    return metrics


# =============================================================================
# MAIN
# =============================================================================
def run():
    symbols = [s.upper() for s in sys.argv[1:]] if len(sys.argv) > 1 else ['SPY']

    t_start = time.time()
    print("=" * 80)
    print(f"  QUANTFOLIO — BUY-ONLY vs FULL SIGNAL BACKTEST")
    print(f"  Symbols: {', '.join(symbols)}")
    print("=" * 80)
    print(f"  Full Signal:  BUY when Z >= {THRESHOLD}, SELL when Z <= -{THRESHOLD}")
    print(f"  Buy-Only:     BUY when Z >= {THRESHOLD}, never sell (hold forever)")
    print(f"  Buy & Hold:   buy on day 1")

    all_metrics = {}

    for sym in symbols:
        print(f"\n{'─'*80}")
        print(f"  {sym}")
        print(f"{'─'*80}")

        print(f"\n  Loading {sym} data...")
        df_raw = load_data(sym)
        print(f"  {len(df_raw)} rows: {df_raw.index[0].date()} to {df_raw.index[-1].date()}")

        print(f"\n  Engineering features...")
        df_lite = engineer_lite(df_raw.copy())
        df_pro = engineer_pro(df_raw.copy())

        print(f"\n  Running Pro model...")
        res_pro = run_backtest(df_pro, PRO_COLS, 'Pro', sym)

        print(f"\n  Running Lite model...")
        res_lite = run_backtest(df_lite, LITE_COLS, 'Lite', sym)

        # Align lengths
        min_len = min(len(res_pro['full']), len(res_lite['full']))
        dates = res_pro['dates'][:min_len]

        results_dict = {
            'Pro Full': res_pro['full'][:min_len],
            'Pro Buy-Only': res_pro['buy_only'][:min_len],
            'Lite Full': res_lite['full'][:min_len],
            'Lite Buy-Only': res_lite['buy_only'][:min_len],
            'B&H': res_pro['bnh'][:min_len],
        }
        entries = {'Pro': res_pro['bo_entry'], 'Lite': res_lite['bo_entry']}

        m = print_table(sym, dates, results_dict, entries)
        all_metrics[sym] = m

        plot_symbol(sym, dates, results_dict)

    # Summary across symbols
    if len(symbols) > 1:
        print(f"\n{'='*80}")
        print(f"  CROSS-SYMBOL SUMMARY")
        print(f"{'='*80}\n")
        print(f"  {'Symbol':8s} {'Pro Full':>10s} {'Pro BO':>10s} {'Lite Full':>10s} {'Lite BO':>10s} {'B&H':>10s}")
        print(f"  {'─'*58}")
        for sym in symbols:
            m = all_metrics[sym]
            print(f"  {sym:8s}"
                  f" {m['Pro Full']['sharpe']:>10.2f}"
                  f" {m['Pro Buy-Only']['sharpe']:>10.2f}"
                  f" {m['Lite Full']['sharpe']:>10.2f}"
                  f" {m['Lite Buy-Only']['sharpe']:>10.2f}"
                  f" {m['B&H']['sharpe']:>10.2f}")
        print(f"  {'─'*58}")

        # Averages
        avg = {}
        for name in ['Pro Full', 'Pro Buy-Only', 'Lite Full', 'Lite Buy-Only', 'B&H']:
            avg[name] = np.mean([all_metrics[s][name]['sharpe'] for s in symbols])
        print(f"  {'Average':8s}"
              f" {avg['Pro Full']:>10.2f}"
              f" {avg['Pro Buy-Only']:>10.2f}"
              f" {avg['Lite Full']:>10.2f}"
              f" {avg['Lite Buy-Only']:>10.2f}"
              f" {avg['B&H']:>10.2f}")

    total_time = time.time() - t_start
    print(f"\n  Total wall time: {total_time:.0f}s ({total_time/60:.1f}min)")
    print(f"\n{'='*80}")
    print(f"  DONE")
    print(f"{'='*80}")


if __name__ == '__main__':
    run()
    sys.stdout.flush()
