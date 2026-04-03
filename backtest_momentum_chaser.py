"""
Quantfolio — Momentum Chaser Backtest
=======================================
Strategy:
  1. On day N, find the top gainer (by daily % return) from the ticker pool
  2. Buy that ticker at close price of day N with all cash
  3. Hold overnight, sell at close price of day N+1
  4. Repeat: buy the top gainer of day N+1, sell at close of day N+2
  5. Compare against: Buy & Hold SPY, and a "top 3 average" variant

Usage:
  python backtest_momentum_chaser.py
"""

import pandas as pd
import numpy as np
import os
import warnings
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION
# =============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "data_cache")
TICKERS_CSV = os.path.join(SCRIPT_DIR, "Tickers.csv")
BACKTEST_START = '2010-01-04'
INITIAL_CASH = 10000
OUTPUT_CHART = os.path.join(SCRIPT_DIR, "momentum_chaser_backtest.png")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "momentum_chaser_results.csv")


# =============================================================================
# LOAD DATA
# =============================================================================

def load_tickers():
    """Load ticker symbols from Tickers.csv."""
    tickers = []
    with open(TICKERS_CSV, 'r', encoding='utf-8-sig') as f:
        for line in f:
            t = line.strip().upper()
            if t and not t.startswith('#'):
                tickers.append(t)
    return list(dict.fromkeys(tickers))  # deduplicate, preserve order


def load_all_prices(tickers):
    """Load Close and Open prices for all tickers into DataFrames."""
    close_frames = {}
    open_frames = {}
    for t in tickers:
        path = os.path.join(CACHE_DIR, f"{t}.csv")
        if not os.path.exists(path):
            print(f"  [skip] {t} — no cached data")
            continue
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            if 'Close' in df.columns and 'Open' in df.columns and len(df) > 100:
                close_frames[t] = df['Close'].squeeze()
                open_frames[t] = df['Open'].squeeze()
            else:
                print(f"  [skip] {t} — insufficient data")
        except Exception as e:
            print(f"  [skip] {t} — {e}")

    close_df = pd.DataFrame(close_frames)
    close_df.sort_index(inplace=True)
    open_df = pd.DataFrame(open_frames)
    open_df.sort_index(inplace=True)
    return close_df, open_df


# =============================================================================
# DAILY RETURNS
# =============================================================================

def compute_daily_returns(close_df):
    """Compute daily percentage returns for all tickers."""
    return close_df.pct_change()


# =============================================================================
# MOMENTUM CHASER STRATEGY
# =============================================================================

def run_momentum_chaser(close_df, returns_df, backtest_start, initial_cash,
                         top_n=1, strategy_name="Top 1 Gainer"):
    """
    Strategy:
      Each day, identify the top N gainer(s) by daily return.
      Buy equally at close, hold overnight, sell at next day's close.

    Args:
      top_n: 1 = buy only the top gainer; 3 = spread across top 3; etc.
    """
    start_date = pd.Timestamp(backtest_start)
    mask = returns_df.index >= start_date
    bt_dates = returns_df.index[mask]

    cash = initial_cash
    portfolio = []
    picked_tickers = []
    daily_pnl = []
    dates_out = []

    for i in range(len(bt_dates) - 1):
        today = bt_dates[i]
        tomorrow = bt_dates[i + 1]

        # Today's returns — find top gainers
        today_rets = returns_df.loc[today].dropna()
        if len(today_rets) == 0:
            portfolio.append(cash)
            picked_tickers.append('CASH')
            daily_pnl.append(0.0)
            dates_out.append(today)
            continue

        # Sort by return, pick top N
        top_gainers = today_rets.nlargest(top_n)
        picks = top_gainers.index.tolist()

        # Buy at today's close, sell at tomorrow's close
        # Overnight return = (tomorrow_close / today_close) - 1
        overnight_returns = []
        valid_picks = []
        for ticker in picks:
            today_close = close_df.loc[today, ticker] if today in close_df.index else np.nan
            tmrw_close = close_df.loc[tomorrow, ticker] if tomorrow in close_df.index else np.nan
            if not np.isnan(today_close) and not np.isnan(tmrw_close) and today_close > 0:
                overnight_returns.append(tmrw_close / today_close - 1)
                valid_picks.append(ticker)

        if len(overnight_returns) == 0:
            portfolio.append(cash)
            picked_tickers.append('CASH')
            daily_pnl.append(0.0)
            dates_out.append(today)
            continue

        # Equal weight across valid picks
        avg_return = np.mean(overnight_returns)
        pnl = cash * avg_return
        cash += pnl

        portfolio.append(cash)
        picked_tickers.append(','.join(valid_picks))
        daily_pnl.append(avg_return * 100)
        dates_out.append(today)

    return {
        'dates': dates_out,
        'portfolio': np.array(portfolio),
        'picked_tickers': picked_tickers,
        'daily_pnl': np.array(daily_pnl),
        'strategy_name': strategy_name,
    }


# =============================================================================
# OPEN-TO-OPEN MOMENTUM CHASER — Buy N+1 Open, Sell N+2 Open
# =============================================================================

def run_momentum_open2open(close_df, open_df, returns_df, backtest_start,
                            initial_cash, top_n=1,
                            strategy_name="O2O Top 1"):
    """
    Strategy:
      Day N: Find top N gainer(s) by daily close-to-close return.
      Day N+1: Buy at open.
      Day N+2: Sell at open (hold for ~24 hours including one overnight).

    This captures: intraday session N+1 + overnight gap into N+2.
    """
    start_date = pd.Timestamp(backtest_start)
    mask = returns_df.index >= start_date
    bt_dates = returns_df.index[mask]

    cash = initial_cash
    portfolio = []
    picked_tickers = []
    daily_pnl = []
    dates_out = []

    for i in range(len(bt_dates) - 2):  # need i, i+1, i+2
        today = bt_dates[i]
        tomorrow = bt_dates[i + 1]
        day_after = bt_dates[i + 2]

        # Today's returns — find top gainers
        today_rets = returns_df.loc[today].dropna()
        if len(today_rets) == 0:
            portfolio.append(cash)
            picked_tickers.append('CASH')
            daily_pnl.append(0.0)
            dates_out.append(today)
            continue

        top_gainers = today_rets.nlargest(top_n)
        picks = top_gainers.index.tolist()

        # Buy at tomorrow's OPEN, sell at day_after's OPEN
        o2o_returns = []
        valid_picks = []
        for ticker in picks:
            tmrw_open = open_df.loc[tomorrow, ticker] if tomorrow in open_df.index and ticker in open_df.columns else np.nan
            da_open = open_df.loc[day_after, ticker] if day_after in open_df.index and ticker in open_df.columns else np.nan
            if not np.isnan(tmrw_open) and not np.isnan(da_open) and tmrw_open > 0:
                o2o_returns.append(da_open / tmrw_open - 1)
                valid_picks.append(ticker)

        if len(o2o_returns) == 0:
            portfolio.append(cash)
            picked_tickers.append('CASH')
            daily_pnl.append(0.0)
            dates_out.append(today)
            continue

        avg_return = np.mean(o2o_returns)
        pnl = cash * avg_return
        cash += pnl

        portfolio.append(cash)
        picked_tickers.append(','.join(valid_picks))
        daily_pnl.append(avg_return * 100)
        dates_out.append(today)

    return {
        'dates': dates_out,
        'portfolio': np.array(portfolio),
        'picked_tickers': picked_tickers,
        'daily_pnl': np.array(daily_pnl),
        'strategy_name': strategy_name,
    }


# =============================================================================
# INTRADAY MOMENTUM CHASER — Buy N+1 Open, Sell N+1 Close
# =============================================================================

def run_momentum_intraday(close_df, open_df, returns_df, backtest_start,
                           initial_cash, top_n=1,
                           strategy_name="Intraday Top 1"):
    """
    Strategy:
      Day N: Find top N gainer(s) by daily close-to-close return.
      Day N+1: Buy at open, sell at close (intraday only — no overnight risk).

    Args:
      close_df: DataFrame of close prices
      open_df:  DataFrame of open prices
      top_n: 1 = single best gainer; 3 = spread across top 3; etc.
    """
    start_date = pd.Timestamp(backtest_start)
    mask = returns_df.index >= start_date
    bt_dates = returns_df.index[mask]

    cash = initial_cash
    portfolio = []
    picked_tickers = []
    daily_pnl = []
    dates_out = []

    for i in range(len(bt_dates) - 1):
        today = bt_dates[i]
        tomorrow = bt_dates[i + 1]

        # Today's returns — find top gainers
        today_rets = returns_df.loc[today].dropna()
        if len(today_rets) == 0:
            portfolio.append(cash)
            picked_tickers.append('CASH')
            daily_pnl.append(0.0)
            dates_out.append(today)
            continue

        top_gainers = today_rets.nlargest(top_n)
        picks = top_gainers.index.tolist()

        # Buy at tomorrow's OPEN, sell at tomorrow's CLOSE
        # Intraday return = (tomorrow_close / tomorrow_open) - 1
        intraday_returns = []
        valid_picks = []
        for ticker in picks:
            tmrw_open = open_df.loc[tomorrow, ticker] if tomorrow in open_df.index and ticker in open_df.columns else np.nan
            tmrw_close = close_df.loc[tomorrow, ticker] if tomorrow in close_df.index and ticker in close_df.columns else np.nan
            if not np.isnan(tmrw_open) and not np.isnan(tmrw_close) and tmrw_open > 0:
                intraday_returns.append(tmrw_close / tmrw_open - 1)
                valid_picks.append(ticker)

        if len(intraday_returns) == 0:
            portfolio.append(cash)
            picked_tickers.append('CASH')
            daily_pnl.append(0.0)
            dates_out.append(today)
            continue

        avg_return = np.mean(intraday_returns)
        pnl = cash * avg_return
        cash += pnl

        portfolio.append(cash)
        picked_tickers.append(','.join(valid_picks))
        daily_pnl.append(avg_return * 100)
        dates_out.append(today)

    return {
        'dates': dates_out,
        'portfolio': np.array(portfolio),
        'picked_tickers': picked_tickers,
        'daily_pnl': np.array(daily_pnl),
        'strategy_name': strategy_name,
    }


# =============================================================================
# METRICS
# =============================================================================

def compute_metrics(portfolio, initial_cash, strategy_name):
    total_ret = (portfolio[-1] / initial_cash - 1) * 100
    n_years = len(portfolio) / 252
    annual_ret = ((portfolio[-1] / initial_cash) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

    daily_rets = np.diff(portfolio) / portfolio[:-1]
    sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * np.sqrt(252)) if np.std(daily_rets) > 0 else 0

    peak = np.maximum.accumulate(portfolio)
    dd = (portfolio - peak) / peak
    max_dd = float(dd.min()) * 100

    # Win rate
    win_days = np.sum(daily_rets > 0)
    total_days = len(daily_rets)
    win_rate = win_days / total_days * 100 if total_days > 0 else 0

    # Average win / average loss
    wins = daily_rets[daily_rets > 0]
    losses = daily_rets[daily_rets < 0]
    avg_win = float(np.mean(wins) * 100) if len(wins) > 0 else 0
    avg_loss = float(np.mean(losses) * 100) if len(losses) > 0 else 0
    profit_factor = abs(np.sum(wins) / np.sum(losses)) if len(losses) > 0 and np.sum(losses) != 0 else 0

    return {
        'strategy': strategy_name,
        'final_value': round(portfolio[-1], 2),
        'total_return': round(total_ret, 1),
        'annual_return': round(annual_ret, 1),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 1),
        'win_rate': round(win_rate, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'n_days': len(portfolio),
    }


def compute_bnh_spy(close_df, backtest_start, initial_cash):
    """Buy & Hold SPY benchmark."""
    if 'SPY' not in close_df.columns:
        return None, None
    spy = close_df['SPY'].dropna()
    spy = spy[spy.index >= pd.Timestamp(backtest_start)]
    if len(spy) < 10:
        return None, None
    bnh = initial_cash * (spy / spy.iloc[0]).values
    return bnh, spy.index


# =============================================================================
# CHART
# =============================================================================

def plot_momentum_chaser(results_list, bnh_equity, bnh_dates, output_path):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), sharex=True,
                                    gridspec_kw={'height_ratios': [3, 1]})

    colors = ['#FF5722', '#2196F3', '#4CAF50', '#9C27B0', '#FF9800', '#00BCD4', '#E91E63']

    # Equity curves
    for i, res in enumerate(results_list):
        dates_plot = pd.to_datetime(res['dates'])
        ax1.plot(dates_plot, res['portfolio'], label=res['strategy_name'],
                 linewidth=1.3, color=colors[i % len(colors)])

    if bnh_equity is not None:
        ax1.plot(pd.to_datetime(bnh_dates), bnh_equity, label='Buy & Hold SPY',
                 linewidth=1.0, color='#9E9E9E', linestyle='--')

    ax1.set_ylabel('Portfolio Value ($)')
    ax1.set_title('Momentum Chaser: Overnight (Close-to-Close) vs Open-to-Open')
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # Drawdown
    for i, res in enumerate(results_list):
        dates_plot = pd.to_datetime(res['dates'])
        port = res['portfolio']
        peak = np.maximum.accumulate(port)
        dd = (port - peak) / peak * 100
        ax2.fill_between(dates_plot, dd, 0, alpha=0.3, color=colors[i % len(colors)],
                         label=res['strategy_name'])
        ax2.plot(dates_plot, dd, linewidth=0.8, color=colors[i % len(colors)])

    if bnh_equity is not None:
        peak = np.maximum.accumulate(bnh_equity)
        dd = (bnh_equity - peak) / peak * 100
        ax2.fill_between(pd.to_datetime(bnh_dates), dd, 0, alpha=0.2,
                         color='#9E9E9E', label='B&H SPY')
        ax2.plot(pd.to_datetime(bnh_dates), dd, linewidth=0.8, color='#9E9E9E')

    ax2.set_ylabel('Drawdown (%)')
    ax2.set_xlabel('Date')
    ax2.legend(loc='lower left', fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\n  Chart saved: {output_path}")


# =============================================================================
# MAIN
# =============================================================================

def run_backtest():
    print("=" * 70)
    print("  QUANTFOLIO — MOMENTUM CHASER BACKTEST")
    print("=" * 70)
    print(f"  ON  = Overnight:    Buy Day N close,  Sell Day N+1 close")
    print(f"  O2O = Open-to-Open: Buy Day N+1 open, Sell Day N+2 open")
    print(f"  Starting capital: ${INITIAL_CASH:,}")
    print(f"  Backtest from: {BACKTEST_START}")

    # Load tickers
    print(f"\n[1/4] Loading tickers...")
    tickers = load_tickers()
    print(f"  {len(tickers)} tickers in pool")

    # Load prices (close + open)
    print(f"\n[2/4] Loading price data from cache...")
    close_df, open_df = load_all_prices(tickers)
    print(f"  Loaded {len(close_df.columns)} tickers, {len(close_df)} trading days")
    print(f"  Date range: {close_df.index[0].date()} to {close_df.index[-1].date()}")

    # Compute returns
    returns_df = compute_daily_returns(close_df)

    # Run strategies
    print(f"\n[3/4] Running backtests...")

    # --- OVERNIGHT strategies ---
    print(f"\n  === OVERNIGHT: Buy Day N close, Sell Day N+1 close ===")

    print(f"\n  --- ON Top 1 ---")
    res_on1 = run_momentum_chaser(close_df, returns_df, BACKTEST_START,
                                   INITIAL_CASH, top_n=1,
                                   strategy_name="ON Top 1")

    print(f"\n  --- ON Top 2 ---")
    res_on2 = run_momentum_chaser(close_df, returns_df, BACKTEST_START,
                                   INITIAL_CASH, top_n=2,
                                   strategy_name="ON Top 2")

    print(f"\n  --- ON Top 3 ---")
    res_on3 = run_momentum_chaser(close_df, returns_df, BACKTEST_START,
                                   INITIAL_CASH, top_n=3,
                                   strategy_name="ON Top 3")

    # --- OPEN-TO-OPEN strategies ---
    print(f"\n  === OPEN-TO-OPEN: Buy Day N+1 open, Sell Day N+2 open ===")

    print(f"\n  --- O2O Top 1 ---")
    res_o2o1 = run_momentum_open2open(close_df, open_df, returns_df, BACKTEST_START,
                                       INITIAL_CASH, top_n=1,
                                       strategy_name="O2O Top 1")

    print(f"\n  --- O2O Top 2 ---")
    res_o2o2 = run_momentum_open2open(close_df, open_df, returns_df, BACKTEST_START,
                                       INITIAL_CASH, top_n=2,
                                       strategy_name="O2O Top 2")

    print(f"\n  --- O2O Top 3 ---")
    res_o2o3 = run_momentum_open2open(close_df, open_df, returns_df, BACKTEST_START,
                                       INITIAL_CASH, top_n=3,
                                       strategy_name="O2O Top 3")

    # Buy & Hold SPY
    bnh_equity, bnh_dates = compute_bnh_spy(close_df, BACKTEST_START, INITIAL_CASH)

    # Compute metrics
    print(f"\n[4/4] Computing metrics...")
    results_list = [res_on1, res_on2, res_on3, res_o2o1, res_o2o2, res_o2o3]
    metrics = [compute_metrics(r['portfolio'], INITIAL_CASH, r['strategy_name'])
               for r in results_list]

    if bnh_equity is not None:
        m_bnh = compute_metrics(bnh_equity, INITIAL_CASH, "B&H SPY")
        metrics.append(m_bnh)

    # Print results
    print(f"\n{'=' * 120}")
    print(f"  MOMENTUM CHASER RESULTS: {BACKTEST_START} -> {res_on1['dates'][-1].date()}")
    print(f"  ON = Overnight (close->close)  |  O2O = Open-to-Open (open->open)")
    print(f"{'=' * 120}")
    print()
    header_strats = [m['strategy'] for m in metrics]
    print(f"  {'':22s}", end="")
    for s in header_strats:
        print(f"  {s:>16s}", end="")
    print()
    print(f"  {'─' * (22 + 18 * len(metrics))}")

    rows = [
        ('Final Value', 'final_value', lambda v: f"${v:>13,.0f}"),
        ('Total Return', 'total_return', lambda v: f"{v:>+15.1f}%"),
        ('Annual Return', 'annual_return', lambda v: f"{v:>+15.1f}%"),
        ('Sharpe Ratio', 'sharpe', lambda v: f"{v:>16.2f}"),
        ('Max Drawdown', 'max_drawdown', lambda v: f"{v:>15.1f}%"),
        ('Win Rate', 'win_rate', lambda v: f"{v:>15.1f}%"),
        ('Avg Win', 'avg_win', lambda v: f"{v:>+15.2f}%"),
        ('Avg Loss', 'avg_loss', lambda v: f"{v:>+15.2f}%"),
        ('Profit Factor', 'profit_factor', lambda v: f"{v:>16.2f}"),
        ('Trading Days', 'n_days', lambda v: f"{v:>16,d}"),
    ]

    for label, key, fmt in rows:
        print(f"  {label:22s}", end="")
        for m in metrics:
            val = m.get(key, 0)
            if val is not None:
                print(f"  {fmt(val)}", end="")
            else:
                print(f"  {'--':>16s}", end="")
        print()

    print(f"  {'─' * (22 + 18 * len(metrics))}")

    # Most picked tickers analysis
    for res, label in [(res_on1, "Overnight Top 1"), (res_o2o1, "O2O Top 1")]:
        print(f"\n  TOP 10 MOST PICKED — {label}:")
        print(f"  {'─' * 40}")
        from collections import Counter
        ticker_counts = Counter(res['picked_tickers'])
        for ticker, count in ticker_counts.most_common(10):
            pct = count / len(res['picked_tickers']) * 100
            print(f"    {ticker:<8s}  {count:>4d} days  ({pct:>5.1f}%)")

    # Chart
    plot_momentum_chaser(results_list, bnh_equity, bnh_dates, OUTPUT_CHART)

    # Save detailed daily logs
    for res, suffix in [(res_on1, "overnight"), (res_o2o1, "open2open")]:
        log_path = os.path.join(SCRIPT_DIR, f"momentum_chaser_{suffix}.csv")
        log_df = pd.DataFrame({
            'Date': res['dates'],
            'Portfolio': res['portfolio'],
            'Picked_Ticker': res['picked_tickers'],
            'Daily_PnL_Pct': res['daily_pnl'],
        })
        log_df.to_csv(log_path, index=False)
        print(f"  {suffix.title()} log saved: {log_path}")

    print(f"\n{'=' * 70}")
    print(f"  DONE")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    run_backtest()
