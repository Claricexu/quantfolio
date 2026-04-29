# Quantfolio User Guide

A friendly, step-by-step guide for everyday users. No coding experience required.

---

## What is Quantfolio?

Quantfolio is a tool that helps you decide **what to own and when to buy or sell**. It has two layers working together: a **fundamentals screener** that scans the entire SEC-registered US equity universe for business-quality leaders, and a pair of **machine-learning models** that predict next-day price moves for the leaders plus your manual watchlist. Results are shown side-by-side with a clear consensus signal.

For every ticker you look at, Quantfolio tells you:

1. **A predicted price** — what each model expects tomorrow's close to be
2. **A consensus signal** — BUY, SELL, or HOLD based on whether both models agree
3. **A confidence score** — HIGH, MEDIUM, NEUTRAL, or CONFLICT
4. **A valuation check** (SVR) — is the stock cheap, fairly priced, or expensive
5. **The best historical strategy** — which trading approach has worked best for this ticker over the last 10+ years

Everything runs locally on your computer through a dashboard in your web browser. No cloud, no subscriptions, no logins.

### Two models, always compared

| Model | Features | Under the hood |
|---|---|---|
| **Lite** | 13 | Random Forest + XGBoost |
| **Pro** | 22 | LightGBM + XGBoost + Random Forest (stacking ensemble) |

You don't pick one or the other — Quantfolio always runs **both** and shows you the result side-by-side. When they agree, that's a HIGH-confidence signal. When they disagree, the confidence is lower and you know to be cautious.

---

## Part 1 — First-Time Setup (one-time, ~10 minutes)

You only do this once on a new computer.

### Step 1: Install Python

1. Go to https://www.python.org/downloads/
2. Download **Python 3.11** or newer
3. Run the installer
4. **Important:** On the first screen, check the box that says **"Add Python to PATH"**
5. Click Install Now and wait for it to finish

### Step 2: Get the Quantfolio code

If you got this from GitHub:

1. Go to the GitHub page for Quantfolio
2. Click the green **Code** button → **Download ZIP**
3. Unzip it to a folder you'll remember, like `C:\Users\YourName\Documents\Quantfolio`

If it's already on your computer, skip to Step 3.

### Step 3: Install the tools Quantfolio needs

1. Open the Quantfolio folder in File Explorer
2. Click on the address bar at the top, type `cmd`, and press Enter. A black window (Command Prompt) opens, already in the right folder.
3. Type this command and press Enter:
   ```
   pip install -r requirements.txt
   ```
4. Wait a few minutes. You'll see a lot of text — that's normal.
5. When it's done and you see the prompt again, you're ready.

### Step 4: Configure email alerts and SEC contact (optional but recommended)

Quantfolio uses a file called `.env` to store local settings like your email password. It's private to your computer and never uploaded to GitHub. Two things can go in there:

1. **Email alerts** — so Quantfolio can email you HIGH-confidence BUY/SELL signals after each 4:05 PM scan.
2. **SEC contact email** — so the Leader Detector can politely identify itself to SEC EDGAR (required by SEC; without a real email, SEC rate-limits the screener).

**How to set it up:**

1. In the Quantfolio folder, find the file `.env.example`. Make a copy of it and rename the copy to `.env` (just `.env`, no extension).
2. Open `.env` in Notepad or any text editor.
3. Fill in the values you want.

   **For email alerts (Gmail):**
   - Go to https://myaccount.google.com/apppasswords — requires 2-Step Verification on your Gmail.
   - Generate a new App Password called "Quantfolio". You'll get a 16-character string like `abcd efgh ijkl mnop`.
   - In `.env`, set:
     ```
     SMTP_ENABLED=true
     SMTP_USER=your-email@gmail.com
     SMTP_PASSWORD=abcd efgh ijkl mnop
     ALERT_TO=your-email@gmail.com
     ```
   - Don't want email alerts? Leave `SMTP_ENABLED=false` and skip the rest of the email fields.

   **For SEC EDGAR contact:**
   - Put your real email in both `SEC_USER_AGENT` and `SEC_CONTACT_EMAIL`. Example:
     ```
     SEC_USER_AGENT=Quantfolio my-email@example.com
     SEC_CONTACT_EMAIL=my-email@example.com
     ```
   - If you skip this, the Leader Detector will use a generic fallback that SEC will rate-limit or block.

4. Save `.env` and close the editor.

That's it. You never have to do Steps 1-4 again unless you move to a new computer.

---

## Part 2 — Launching the Dashboard

**The easy way (Windows):**
- Double-click `start_dashboard.bat` in the Quantfolio folder
- A black window opens — **leave it open** as long as you're using Quantfolio
- Open your browser and go to **http://localhost:8000**

**The manual way:**
- Open Command Prompt in the Quantfolio folder
- Type `python api_server.py` and press Enter
- Open your browser and go to **http://localhost:8000**

You'll see four tabs at the top: **Ticker Lookup**, **Daily Report**, **Strategy Lab**, and **Leader Detector**.

---

## Part 3 — The Ticker Lookup Tab

This is where you check a single ticker on demand.

### How to use it

1. Type a ticker symbol in the input box (e.g., `SPY`, `AAPL`, `NVDA`, `TSM`)
2. Click **Predict** (or press Enter)
3. Wait a few seconds — the first time you look up a ticker, both models need to train

If that ticker was already processed by the most recent Daily Report run, you'll see a small green **CACHED** pill next to the "Through" date, and the result appears almost instantly (~50ms instead of ~30 seconds). This is the smart fast-path — same data, zero waiting. Predictions cached from the most recent Daily Report run are reused in Ticker Lookup including across weekends, so a Friday-afternoon report stays available through Saturday and Sunday until the next scheduled Monday run. The cache is also restored from disk on the first lookup after a server restart, so you don't pay the recompute cost just because you closed and reopened the dashboard.

### What the result shows

**Header**
- **Ticker symbol** and today's closing price
- **"Through" date** — the last trading day the prediction uses
- **CACHED pill** (if applicable) — result came from today's Daily Report

**Consensus box**
- **Consensus Signal** — BUY, SELL, or HOLD — agreed by both models
- **Confidence** — HIGH / MEDIUM / NEUTRAL / CONFLICT
- **Best Strategy** — the historically best-performing strategy for this ticker, with its Sharpe ratio

**Side-by-side Lite vs Pro panels**
- **Predicted price** — where the model thinks the stock will close tomorrow
- **Change %** — predicted move in percent
- **Signal** — that individual model's BUY / SELL / HOLD call
- **Dir Accuracy** — how often this model got the direction (up/down) correct on the holdout set
- **Z-Score** — how unusual today's prediction is vs the last 126 days (±2.5σ triggers signals)
- **Sub-models** — individual predictions from RF, XGB, and LGBM before blending

**Valuation cards (4 cards at the top)**
- **SVR** (Simple Value Ratio) — Market Cap ÷ Annual Revenue. Shown with a color-coded hint:
  - Green = Undervalued (SVR < 3)
  - Yellow = Fair Value (3 ≤ SVR < 8)
  - Red = Expensive (SVR ≥ 8)
- **Market Cap** — company size in $M/$B/$T
- **Quarterly Revenue** — most recent quarter's revenue
- **P/E** — trailing Price-to-Earnings ratio. Em-dash for loss-making companies and missing data. For ETFs, hover the value to see "Weighted average of holdings" — an ETF's P/E is a holdings aggregate, not a valuation signal in the usual sense.

**Verdict card (below the valuation cards)**
- Three-column grid: Metric / Company / Peer Median. The Peer Median column shows the median value across other companies in the same industry group, so you can see at a glance whether this company sits above or below its peers on Revenue Growth, Operating Margin, ROIC, etc.
- Em-dash in the Peer Median column means the metric isn't comparable for this row (Sector, Industry Group, Industry are categorical) or the industry-group bucket has fewer than 5 companies reporting that metric.
- Sector / Industry Group / Industry rows show the canonical classification (e.g., NVDA → Technology / Semiconductors / Semiconductors & Related Devices). Override tickers (GOOGL, META, AMZN, AAPL, TSLA, V, MA, NFLX, GOOG) use a hand-crafted Industry value rather than the SEC's filed description.
- A small "as of" timestamp chip sits above the SCORE box. Hover to reveal the raw ISO timestamp — useful if you suspect stale data.
- For ETFs, an inline note "Peer median comparison not applicable for ETFs." appears below the grid.

**Backtest Comparison button**
- Click it to load an interactive chart of all 5 backtest strategies on a $10,000 portfolio, walking forward from 2015.

### What consensus and confidence mean

| Both models say... | Consensus | Confidence |
|---|---|---|
| BUY + BUY | BUY | **HIGH** |
| SELL + SELL | SELL | **HIGH** |
| HOLD + HOLD | HOLD | NEUTRAL |
| BUY + HOLD (or HOLD + BUY) | whichever is non-HOLD | MEDIUM |
| SELL + HOLD (or HOLD + SELL) | whichever is non-HOLD | MEDIUM |
| BUY + SELL | HOLD | **CONFLICT** |

**Rule of thumb:** Act on HIGH-confidence signals. Treat MEDIUM as "worth watching." Ignore CONFLICT — the models disagree and neither has a clear edge.

### Direction Accuracy vs Confidence — what's the difference?

- **Dir Accuracy** is measured per model. It's the percentage of days where that model correctly predicted up vs. down on the 15% validation holdout. A value above 60% is great; 50% is random chance.
- **Confidence** is about *agreement between the two models today*, not accuracy. A HIGH-confidence BUY means Lite and Pro both say BUY today — regardless of how accurate they've been historically.

Use them together: a HIGH-confidence signal from two models with strong Dir Accuracy scores is the strongest possible read.

---

## Part 4 — The Daily Report Tab

The Daily Report auto-scans **all 174 symbols** (100 automated leaders plus your manual watchlist) at once and shows you a sortable ranking of opportunities.

### When it runs

- **Automatically** at **4:05 PM EST** on trading days (Monday-Friday), just after US market close
- **Manually** by clicking the **Refresh** button on the Daily Report tab (typically 25-55 minutes on a laptop; the UI banner shows the same band)

> **Editor's note (2026-04-23):** the 25-55 minute band is a conservative guess, not a measured value — Round 2's attempt to time one end-to-end scan was aborted before completion. Expect real runs to land somewhere inside this band on a modern laptop, but treat the number as a placeholder until a fresh measurement lands.

### What's on the page

**Summary callout at top**
- Market sentiment (BULLISH / BEARISH / MIXED)
- Counts of BUY / SELL / HOLD signals across the universe
- HIGH-confidence count and model-conflict count

**High-Confidence BUY section**
- Only tickers where **both Lite and Pro agree on BUY**
- Sorted by average predicted gain
- Shows symbol, current price, both model predictions, best strategy

**High-Confidence SELL section**
- Only tickers where **both Lite and Pro agree on SELL** — AND where the ticker's best historical strategy is Full Signal
- SELLs on stocks whose best strategy is Buy-Only or Buy & Hold are filtered out, because acting on a SELL for those would have hurt returns historically

**Full sortable table**
- Click any column header to sort (▲ ascending / ▼ descending)
- Click any row in the Daily Report table to expand the verdict card beneath that row. Only one card is open at a time — clicking a second row collapses the first. Close the card with the × button at the top-right or by clicking the same row again.
- Columns (10 total): Symbol, Price, Lite Chg, Lite Sig, Pro Chg, Pro Sig, Consensus, Conf, Best Strategy, Firm Score
- Color-coded signals and confidence badges
- A banner above the tables shows when each row's close price is from. If all symbols share a date the banner reads "Close prices as of YYYY-MM-DD"; if a few rows are stale (e.g., one ticker's data is a day behind), they're broken out as "N symbols' close price as of YYYY-MM-DD" clauses.

### Email alerts (optional)

If you've configured SMTP credentials in your local `.env` file (see **Part 1, Step 4** for how to set them up), Quantfolio sends you an email called **"Quantfolio Signal Brief"** right after every scheduled 4:05 PM run — but only if there's at least one HIGH-confidence signal.

The email includes:
- All HIGH-confidence BUYs
- All HIGH-confidence SELLs (filtered to Full-Signal tickers, same as the dashboard)
- Predicted prices, percent changes, and best strategies for each

No signals → no email. You won't get noise.

---

## Part 5 — The Strategy Lab Tab

This is the heavy lifter. Strategy Lab runs **5 different backtest strategies** on every ticker and shows you which one wins.

### The 5 strategies

| Strategy | What it does |
|---|---|
| **Buy & Hold** | Buy once, hold forever. The benchmark. |
| **Lite Buy-Only** | Lite model. BUY on positive signal, never sell. |
| **Lite Full Signal** | Lite model. BUY and SELL on signals. |
| **Pro Buy-Only** | Pro model. BUY on positive signal, never sell. |
| **Pro Full Signal** | Pro model. BUY and SELL on signals. |

Every backtest starts with $10,000 on January 2, 2015 (or the ticker's IPO + 6 months, whichever is later) and walks forward day-by-day to today. Models retrain every 63 trading days to stay current without looking into the future.

### The "Run All Backtests" button

Click this to process every ticker in your universe. The system is smart about it:

- Tickers with a cached result less than **7 days old** are **skipped** (marked as "already cached")
- Only tickers with no cache or a stale one (≥ 7 days) are re-run
- Lite and Pro run in parallel for each ticker (roughly halves the wall time)
- Expect ~4-8 minutes per uncached ticker

A progress bar shows which ticker is currently running. The page polls the server every 5 seconds during a batch (the equity-curve chart poll is faster, every 3 seconds), so you can safely refresh the browser or switch tabs — the batch keeps going on the server.

### Strategy Recommendations (horizontal bar chart)

At the top of the Strategy Lab, a summary callout shows how many tickers favor each strategy as their best (highest Sharpe). Use this to see where the models genuinely add value and where Buy & Hold is hard to beat.

### Default filter

Strategy Lab opens filtered to the symbols in the most recent Daily Report. A "Show all symbols" toggle above the library bypasses the filter and shows every cached backtest. The toggle resets to off on each page reload — the default is always "today's Daily Report symbols". A small status line on the right reports the current state (e.g., "Filtered to 174 of 312 symbols (current Daily Report)").

### Library Table

Every processed ticker appears with these columns (click any header to sort):

| Column | Meaning |
|---|---|
| **Symbol** | The ticker |
| **Best Strategy** | The highest-Sharpe strategy for this ticker |
| **Best Return** | Total return of that winning strategy |
| **B&H Return** | Total return of plain Buy & Hold for comparison |
| **Best Sharpe** | Risk-adjusted return of the winner |
| **B&H Sharpe** | Risk-adjusted return of Buy & Hold |
| **Sharpe Edge** | How much better (in Sharpe points) the winner is vs B&H — always shown with `+` because the "best" is never worse than B&H by definition |
| **Cached** | The date the backtest was last run (YYYY-MM-DD) |

### Equity Curve Viewer

Click any ticker row in the library table to expand its interactive chart beneath that row. Only one chart is open at a time — clicking a second row collapses the first. Close the chart with the × button at the top-right or by clicking the same row again. You'll see all 5 strategies drawn as portfolio value over time:

- **Buy & Hold** — gray dashed line (benchmark)
- **Lite Buy-Only** — warm yellow
- **Lite Full Signal** — warm coral
- **Pro Buy-Only** — cool seafoam
- **Pro Full Signal** — cool teal

Warm colors = Lite models. Cool colors = Pro models. The dashed gray is always Buy & Hold so you can eyeball whether the model adds value.

---

## Part 6 — The Leader Detector Tab

Quantfolio runs an automated fundamentals screen over the entire SEC-registered US equity universe (~2,500 companies) and distills it down to **100 high-quality leaders** that join your manual watchlist. The Leader Detector tab is where you see what the screener saw.

### What's on the page

**The table** — 1,414 prescreened symbols (market cap ≥ $1B, price > $3, average daily dollar volume > $1M). Columns (in on-screen order):

| Column | What it means |
|---|---|
| **SEL** | ✓ if this ticker is in `leaders.csv` (the 100 Quantfolio trades). A dash (–) means the screener analyzed it but did not pick it. |
| **SYMBOL** | Ticker |
| **NAME** | Company name |
| **SECTOR** | Canonical sector bucket (10 buckets: Technology, Healthcare, Financials, Communication Services, Consumer Discretionary, Consumer Staples, Energy, Industrials, Materials, Utilities). Derived from SIC + ticker overrides for the 9 mega-caps whose SIC codes misrepresent their actual business (GOOGL/META/NFLX → Communication Services, AAPL → Technology, AMZN → Consumer Discretionary, etc.). |
| **MKT CAP** | Size (shown as $M / $B / $T) |
| **VERDICT** | LEADER / GEM / WATCH / AVOID (colored badge). Shows an "as of" timestamp chip reflecting when the screener last ran. |
| **SCORE** | Good Firm score (0–100) |
| **ARCHETYPE** | GROWTH or MATURE |
| **SECTOR RANK** | Market-cap rank within the broad-sector group |

Click any column header to sort (▲ / ▼). Click any row in the Leader Detector table to expand the verdict card beneath that row. Only one card is open at a time — clicking a second row collapses the first. Close the card with the × button at the top-right or by clicking the same row again.

**Filter chips** above the table:

- **VERDICT** — All / Leader / Gem / Watch / Avoid
- **ARCHETYPE** — All / Growth / Mature
- **SECTOR** — dropdown populated from the live data
- **INDUSTRY GROUP** — chip row beneath the controls block. AND-combines with the SECTOR filter, so SECTOR=Technology + INDUSTRY GROUP=Semiconductors narrows to semiconductor companies in the Technology sector. Picking an industry group also trims the Sector dropdown to just the sectors containing rows in that group.

**Rebuild Now** button — kicks off the quarterly pipeline that rebuilds `leaders.csv` from the latest SEC filings.

**Download CSV** button — exports the currently filtered view.

### What the four verdicts mean

| Verdict | Plain English |
|---|---|
| **LEADER** | Passes all 5 business-quality tests **and** is in the top 5 by market cap within its sector. A blue-chip in its industry. |
| **GEM** | Passes all 5 tests but is smaller than the top 5 by market cap. A high-quality runner-up. These are the "hidden gems" — great businesses that aren't household names. |
| **WATCH** | Passes 3 or 4 of the 5 tests, no red flags. Worth keeping an eye on, but not yet strong enough for a position. |
| **AVOID** | Passes 2 or fewer tests, OR has a dealbreaker flag (shrinking revenue, burning cash, heavy dilution). Skip. |

An extra sentinel, **INSUFFICIENT_DATA**, shows up for tickers that don't have enough SEC filing history (typically < 3 years public) to evaluate.

### What GROWTH vs MATURE means

The screener first sorts every company by **revenue growth**:

- **GROWTH** — revenue up ≥ 12% year-over-year. Scored on growth rate, unit economics, path to profits, moat, and capital efficiency. Dealbreaker: burning cash badly (FCF/Revenue < -15%).
- **MATURE** — revenue growing slower than 12% (or declining). Scored on stability, margin quality, cash generation, moat, and not-shrinking. Dealbreakers: declining revenue (3Y CAGR < -5%) or heavy share dilution (> 5% per year).

The split matters because a Coca-Cola should not be graded like a Snowflake. MATURE companies earn their LEADER badge through cash generation and durability; GROWTH companies earn theirs through pace plus a credible path to profits.

### When should I click Rebuild Now?

Almost never — the pipeline runs automatically every quarter (Feb 15 / May 15 / Aug 15 / Nov 15 at 2 AM), right after each 10-Q season ends. Manual rebuilds make sense only if:

- You just added a brand-new ticker that isn't in the screen yet
- The SEC pushed a major filing correction
- You're debugging the pipeline

**Warning:** a cold rebuild takes ~3.5 hours because SEC EDGAR rate-limits API calls to 10 per second. Warm rebuilds (data already cached) finish in ~10 minutes.

### The confirmation modal

Because a cold rebuild is a multi-hour job, clicking **Rebuild Now** no longer starts the pipeline immediately. Instead, an inline confirmation modal opens and asks you to type the token **REBUILD** (case-insensitive — `rebuild`, `Rebuild`, or `REBUILD` all work) before the **Start rebuild** button activates. The modal shows:

- Estimated duration — **~3.5 hours if cold, ~10 minutes if warm** (the pipeline figures out which one it is from the SEC XBRL cache).
- When the last rebuild was, so you can see whether you actually need a fresh one.
- A note that the app is safe to close during the run — the scan continues on the server.

Press **Esc**, click outside the modal, or hit **Cancel** to dismiss without starting anything.

### What the ✓ in the Selected column tells you

A ✓ means this ticker made the cut into `leaders.csv` — the 100-symbol list that feeds Daily Report, Strategy Lab, and the Ticker Lookup universe along with your `Tickers.csv` watchlist. The selection rule is simple: **every LEADER makes it in**, and the remaining slots go to the highest-scoring GEMs until 100 is hit.

A row without a ✓ means the screener analyzed it but didn't pick it (either lower verdict, or a LEADER/GEM that got nudged out by higher-scoring peers when the 100-slot cap filled up).

---

## Part 7 — Understanding Strategy Modes

This is the most important concept to understand. Quantfolio has **three strategy modes**:

| Mode | What it does | When it's best |
|---|---|---|
| **Full Signal** | Generates BUY *and* SELL signals | Broad ETFs (SPY, QQQ, SMH, XLE) |
| **Buy-Only** | Only BUY signals; never sells | Individual stocks (AAPL, MU, NVDA) |
| **Auto** (default) | Picks the right mode automatically | Everything — let the app decide |

### Why two modes?

We ran backtests across many years and many tickers. Here's the pattern:

- **SELL signals help on ETFs.** On SPY, QQQ, and SMH, selling during weak spots added +0.06 to +0.20 to the Sharpe ratio.
- **SELL signals hurt on individual stocks.** On MU, META, and AAPL, the same logic *reduced* Sharpe by -0.14 to -0.35. Individual stocks swing harder, and the model's sell timing ended up losing more than it saved.

**The rule became: buy-and-hold individual stocks, but trade in and out of ETFs.**

**Auto mode** handles this automatically — ETFs get Full Signal, stocks get Buy-Only. You don't need to think about it. It's what the dashboard always uses.

---

## Part 8 — Running Backtests from the Command Line

The Strategy Lab in the dashboard is usually all you need. But if you want a single-ticker deep dive from the command line:

### Quick single-ticker backtest

```
python finance_model_v2.py --backtest SPY
```

Replace `SPY` with any cached ticker.

**Options:**

| Flag | What it does |
|---|---|
| `--strategy full` | Force Full Signal mode (BUY+SELL) |
| `--strategy buy_only` | Force Buy-Only mode |
| `--strategy auto` | Let Quantfolio decide (default) |
| `--version v3` | Use the Pro model |
| `--version v2` | Use the Lite model |

**Example:**
```
python finance_model_v2.py --backtest MU --strategy buy_only --version v3
```

### Old vs New comparison

```
python backtest_old_vs_new.py SPY
```

Compares the original GitHub model to the current improved model for the given ticker. Produces a chart `old_vs_new_SPY.png`.

### Buy-Only vs Full Signal across multiple tickers

```
python backtest_buy_hold.py SPY QQQ AAPL NVDA MSFT
```

Shows a summary table of which strategy works best per ticker.

### Prerequisite for CLI backtests

Each CLI backtest needs a cached CSV. The easiest way to cache a ticker is to just look it up once in the dashboard — that auto-downloads and saves the data.

### Reading the results

```
Metric            Old Lite    New Lite    Old Pro    New Pro    Buy & Hold
Total Return      +36.2%      +135.6%     +282.3%    +431.7%    +285.2%
Annual Return     +2.8%       +7.9%       +12.7%     +16.1%     +12.8%
Sharpe Ratio      0.26        0.59        0.76        0.98       0.77
Max Drawdown      -42.3%      -30.0%      -33.7%     -33.7%     -33.7%
Trades            22          32          1           18         0
```

What they mean:
- **Total Return** — money you'd have made over the whole period
- **Annual Return** — average yearly gain
- **Sharpe Ratio** — risk-adjusted return. Higher is better. Above 1.0 is great, above 0.75 is good.
- **Max Drawdown** — worst peak-to-trough loss during the period
- **Trades** — how many buy/sell events happened

**Rule of thumb:** If the new model has a higher Sharpe *and* a smaller Max Drawdown than Buy & Hold, it's a winner.

### A note on Max Drawdown

You may notice that for volatile single stocks like TSLA, all 5 strategies can show the same Max Drawdown. That's not a bug — the Z-score signal uses a 126-day rolling lookback, so during a slow extended crash the model adapts to the new "normal" and doesn't flag it as extreme. SELL signals protect against sharp spikes, not slow grinds. For long, gradual drawdowns, all strategies ride them down together.

---

## Part 9 — Adding New Tickers

1. Open `Tickers.csv` in the Quantfolio folder with Notepad or any text editor
2. Add the new symbol on its own line (e.g. `ABCD`)
3. Save the file
4. Restart the dashboard (close the black Command Prompt window, then double-click `start_dashboard.bat` again)

The new ticker will appear in the next Daily Report and Strategy Lab run.

**What happens automatically:**
- First lookup/backtest downloads full price history from 2010 onward
- ETFs are detected automatically and assigned Full Signal strategy
- Brand-new IPOs (<6 months of data) won't backtest but will still predict

---

## Part 10 — Upgrading to the Latest Version

When new improvements are pushed to GitHub, here's how to update.

### Step 1: Check for updates

```
git pull
```

You'll see one of these:
- **"Already up to date."** — nothing to do
- **A list of changed files** — new updates were downloaded

### Step 2: Reinstall packages (only if needed)

If the update changed `requirements.txt`:

```
pip install -r requirements.txt
```

When in doubt, running it is safe.

### Step 3: Restart the dashboard

1. Close the Command Prompt window running the dashboard
2. Double-click `start_dashboard.bat` again
3. Hard-refresh the browser (**Ctrl+Shift+R** or **Ctrl+F5**) so it picks up any frontend changes

### If `git pull` doesn't work

You probably don't have Git installed. Either:

1. Install Git from https://git-scm.com/download/win, then use `git pull` from now on, or
2. Re-download the ZIP from GitHub, unzip, and replace your old folder (keep the `data_cache/` folder to preserve your backtest history)

---

## Part 11 — Troubleshooting

| Problem | Fix |
|---|---|
| **"python is not recognized"** | Python wasn't added to PATH. Reinstall Python with the "Add to PATH" box checked. |
| **"ModuleNotFoundError"** | Run `pip install -r requirements.txt` again. |
| **Dashboard won't load in browser** | Make sure the black Command Prompt window is still running. Try http://127.0.0.1:8000 instead of localhost. |
| **"Port 8000 in use"** | Another program is using that port. Close it, or edit `PORT = 8000` inside `api_server.py`. |
| **First scan takes forever** | Normal. A full dual-model scan of all 174 symbols typically takes 25-55 minutes on a laptop (the UI banner and `/api/report` both cite this band). The first run is at the high end because nothing is cached. |
| **Backtest says "file not found"** | The ticker isn't cached yet. Look it up on the dashboard once, then try again. |
| **Best Strategy shows "—"** | Click "Run All Backtests" in Strategy Lab to generate data. |
| **Daily Report shows yesterday's prices** | The scheduled run happens at 4:05 PM EST. Before that, you'll see the previous trading day. Hit Refresh manually after 4:05 PM EST to force an update. |
| **Predict takes 30+ seconds every time** | That's normal if the Daily Report hasn't run today. Once the 4:05 PM scheduled run completes (or you manually refresh), subsequent lookups become instant (with a CACHED pill). |
| **Strategy Lab is empty** | Click "Run All Backtests" to generate results. First full run takes 1-3 hours; subsequent runs are much faster because cached tickers are skipped. |
| **Batch backtest stuck** | Check the black Command Prompt window for errors. Restart the server if needed — already-completed tickers are preserved in cache. |
| **Ctrl+F5 didn't refresh** | Try Ctrl+Shift+R, or close and reopen the browser, or use an incognito/private window. |
| **Model predictions look frozen** | Restart the server. Close the Command Prompt window, double-click `start_dashboard.bat`. |
| **Yahoo Finance rate limit** | Data is cached locally. If Yahoo rate-limits you, wait a few minutes, or delete specific files in `data_cache/` to force refetch. |
| **Leader Detector tab is empty** | Click **Rebuild Now**. First build takes ~3.5 hours (SEC rate limits); quarterly warm rebuilds take ~10 minutes. |
| **Rebuild stuck at "Fetching XBRL"** | Normal — SEC EDGAR limits to 10 requests per second. Don't cancel; it's resume-safe (cached rows skip). |
| **Sector Rank or Archetype column blank** | Brand-new IPOs or micro-caps with < 3 years of filings get INSUFFICIENT_DATA. That's expected, not a bug. |

---

## Part 12 — What Happens Behind the Scenes (optional reading)

You don't need to understand this to use Quantfolio, but if you're curious:

1. **Data fetching.** Quantfolio downloads daily OHLCV price history from Yahoo Finance for every ticker, going back to 2010. Data is saved as CSV files in `data_cache/` so each ticker downloads only once per day.

2. **Feature engineering.** Raw prices are converted into 13 (Lite) or 22 (Pro) "features" — moving averages, RSI, MACD, Bollinger Bands, ATR, OBV, volume z-scores, momentum, volatility measures, and more. These are what the models actually look at.

3. **Training.** Each model studies all historical features up to the current retrain point and learns which patterns tend to come before price moves. Retraining happens every 63 trading days — walking forward, never peeking at the future.

4. **Prediction.** Today's features are fed into the trained model, which outputs a predicted next-day return. That return is translated into a predicted closing price.

5. **Signal generation via Z-score.** The prediction is compared to the last 126 days of predictions (a rolling baseline). If today's prediction is unusually high (Z ≥ +2.5σ), that's a BUY. Unusually low (Z ≤ -2.5σ) is a SELL — but only in Full Signal mode.

6. **SVR filter.** Before a BUY is finalized, Quantfolio checks the Simple Value Ratio (Market Cap ÷ Annualized Revenue). If it's extremely overvalued (SVR > 7), the BUY is cancelled. If it's absurdly overvalued (SVR ≥ 15), a SELL is forced regardless of the model signal.

7. **Consensus scoring.** Both Lite and Pro predictions are combined — identical signals become HIGH confidence, one-sided agreement becomes MEDIUM, direct disagreement becomes CONFLICT (presented as HOLD).

8. **Caching layers that make the dashboard fast:**
   - **Price CSVs** are reused for the same trading day
   - **Daily Report results** are reused in Ticker Lookup until the next scheduled Daily Report run fires — meaning Friday's report stays valid through the weekend, and the cache survives a server restart by reloading from disk
   - **Backtest JSONs** stay fresh for 7 days — the "Run All Backtests" button skips what's still cached
   - **Best-strategy map** is rebuilt on demand from backtest cache files

9. **The scheduled job.** On trading days, an APScheduler cron fires at 4:05 PM EST, runs the full dual-model scan across all 174 symbols, saves the report to disk, fires email alerts (if configured), and warms the Ticker Lookup fast-path cache. By the time you check the dashboard after the close, everything is ready.

10. **The quarterly leader rebuild.** Four times a year (Feb 15 / May 15 / Aug 15 / Nov 15 at 2 AM), a separate cron kicks off the Layer 1 pipeline: it re-pulls the SEC-registered US equity universe, applies the prescreen, fetches the latest XBRL fundamentals, runs every ticker through the archetype-routed Good Firm tests, and writes a fresh `leaders.csv`. The 174-symbol trading universe then refreshes automatically from `leaders.csv` ∪ `Tickers.csv`. You don't do anything — new leaders just show up in Daily Report the next morning.

---

## Part 13 — Quick Reference Card

Print this page or bookmark it.

**Launch the dashboard**
```
Double-click start_dashboard.bat
```
Open http://localhost:8000

**Update to the latest version**
```
git pull
pip install -r requirements.txt
```

**Backtest a single ticker (CLI)**
```
python finance_model_v2.py --backtest SPY
python finance_model_v2.py --backtest MU --strategy buy_only --version v3
```

**Compare old vs new model**
```
python backtest_old_vs_new.py SPY
```

**Compare Buy-Only vs Full Signal across tickers**
```
python backtest_buy_hold.py SPY QQQ AAPL NVDA MSFT
```

**Dashboard tabs at a glance**
| Tab | What it does |
|---|---|
| **Ticker Lookup** | Single-ticker deep dive with both models, consensus, SVR, best strategy, and optional backtest chart |
| **Daily Report** | Market-wide scan of all 174 symbols with HIGH-confidence BUY and SELL sections |
| **Strategy Lab** | Batch backtest library, strategy recommendations, and interactive equity curves |
| **Leader Detector** | View the 1,414-row prescreened universe. Filter by Verdict / Archetype / Sector, click Rebuild Now, Download CSV. |

**Key timings**
- **4:05 PM EST** — Daily Report auto-runs on trading days
- **Feb 15 / May 15 / Aug 15 / Nov 15 at 2 AM** — Quarterly Leader Detector rebuild (after 10-Q season)
- **Until the next scheduled run** — Daily Report cache lifetime in Ticker Lookup (survives weekends and server restart)
- **7 days** — How long a backtest cache file is considered fresh (Strategy Lab reuses it)
- **63 trading days** — How often models retrain inside a backtest

---

## Getting Help

- **Technical details**: See `README.md` for developer-facing documentation
- **Bugs or feature requests**: Open an issue on the Quantfolio GitHub page
- **General questions**: Ask Claude Code — he knows the project inside out

Happy investing!
