# Quantfolio — Two-Layer Architecture Plan

## Context

Quantfolio today has three working tabs (Ticker Lookup, Daily Report, Strategy Lab) plus a Fundamental Screener that operates over a **static 85-ticker `Tickers.csv`** curated manually. That universe is too small, too static, and hand-picked — it cannot answer *"Who are the real industry leaders in the US market right now?"*.

The pivot: turn Quantfolio into a **two-layer system**.

- **Layer 1 — Leader Detector (NEW, to build)**: start from the ~10k US-traded companies, prescreen down to ~500 viable candidates, run the Good Firm Framework on those 500, rank within sector, and output a list of **≤100 Industry Leaders + Potential Leaders**.
- **Layer 2 — Daily Report + Backtest (DONE, do not change)**: the existing prediction / scheduler / backtest pipeline, but now fed by Layer 1's output instead of the static `Tickers.csv`.

**Hard constraint:** Layer 2 code, scheduler, TTL cache, three-tab logic, and the screener verdict card stay frozen until Layer 1 is approved and working. The only Layer 2 touch point is swapping the universe source function (`get_all_symbols()` in `finance_model_v2.py:109`) — a one-line change gated behind Layer 1 shipping.

Decisions locked with user (2026-04-17):
- **Universe source**: SEC's `company_tickers.json` (~10k) filtered by market cap ≥ $1B via yfinance
- **Refresh cadence**: Quarterly auto-rebuild (after 10-Q season)
- **Layer 2 handoff**: Write `leaders.csv`; change `get_all_symbols()` to read it instead of `Tickers.csv` (with fallback)
- **UI**: New 4th tab "Leader Detector"
- **Architecture split** (Option B — Unified Metadata Pass): Phase 1.0 gathers *all* per-ticker metadata; Phase 1.1 is pure local filter logic with zero HTTP calls. Lets you re-tune prescreen criteria and re-run Phase 1.1 in <1 min.
- **Phase 1.0 filter criteria — "investability screen"** (finalized):
  - Market cap ≥ $1B (via yfinance `fast_info.market_cap`)
  - Currency = USD
  - Exchange ≠ foreign / OTC / pink-sheet (reject-list; empty codes accepted since SEC list already filters to US issuers)
  - Annual revenue ≥ $10M (via yfinance `.info['totalRevenue']`)
- **Phase 1.1 prescreen criteria — "framework screen"** (finalized, no HTTP — reads from `universe_raw.csv`):
  - Liquidity: `avg_dollar_volume_90d` ≥ $5M (derived from `fast_info.three_month_average_volume × last_price`, captured in Phase 1.0)
  - Data availability: ≥5 10-Ks AND ≥10 10-Qs (from SEC submissions endpoint, captured in Phase 1.0)
  - Excluded SIC sectors: Banks (6020–6030), REITs (6798), Investment funds / holding shells (6199, 6722, 6770)
  - **Kept in universe** (user decision — archetype tagging will contextualize the verdict): Insurance (6300–6411), Utilities (4911–4939)
  - Hard cap: top 500 by market cap after filters

---

## Architecture

```
┌─────────────────────── LAYER 1: Leader Detector (NEW) ────────────────────────┐
│                                                                                │
│  SEC ~10k company_tickers.json                                                │
│            │                                                                   │
│            ▼                                                                   │
│  [1.0] Unified metadata gather (all HTTP I/O lives here)                       │
│     1a. fast_info → mcap, currency, exchange, 3mo_avg_volume, last_price      │
│     1b. .info → totalRevenue (on ~1400 Stage-1 survivors only)                 │
│     1c. SEC submissions → sic, sic_desc, n_10k, n_10q                          │
│        Investability filter applied inline: mcap≥$1B + USD +                  │
│        non-foreign exchange + revenue≥$10M            → ~1400 rows             │
│        Output: universe_raw.csv (rich metadata, all tickers that pass 1a–1b)  │
│            │                                                                   │
│            ▼                                                                   │
│  [1.1] Pure-local filter (reads universe_raw.csv, ZERO HTTP)                  │
│        6 rules: liquidity ≥ $5M + n_10q > 10 + SIC exclusion +                │
│        blank-SIC reject + SVR ≤ 50 + finance-sector top-50 cap                │
│                                                        → ~1,414 candidates    │
│        (no `target_size` cap in the shipped config)                            │
│            │                                                                   │
│            ▼                                                                   │
│  [1.2] SEC XBRL fetch (reuse edgar_fetcher.py)    → fundamentals.db populated │
│            │                                                                   │
│            ▼                                                                   │
│  [1.3] Good Firm screen + sector rank             → ranked 500                │
│            │                                                                   │
│            ▼                                                                   │
│  [1.4] Leader selection (top ≤100 by rank+verdict) → leaders.csv              │
│                                                                                │
└────────────────────────────────┬──────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────── LAYER 2: Daily Report + Backtest (DONE) ───────────────────┐
│                                                                                │
│  get_all_symbols()  ←  now reads leaders.csv  (was Tickers.csv)               │
│            │                                                                   │
│            ▼                                                                   │
│  Daily prediction scheduler  │  Backtest library  │  Ticker Lookup            │
│  (unchanged)                 │  (unchanged)       │  (unchanged)              │
│                                                                                │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## Layer 1 — Build Plan (~6 working days)

### Phase 1.0 — Unified Metadata Gather (~1 day; partially built)

**Goal**: for every SEC-registered ticker, gather *all* the metadata Phase 1.1 could ever need in a single HTTP-heavy pass. Phase 1.1 is then pure local logic — re-tunable in <1 min with zero network cost.

- **New file:** `universe_builder.py`
- Reuse SEC `company_tickers.json` loader already present in `edgar_fetcher.py:164–176` (pull once, cache)
- **Investability filter applied inline** after Stage 1b: `market_cap >= $1B` AND `currency == USD` AND `exchange ∉ reject-list` AND `annual_revenue >= $10M` → expect ~1400 survivors
- **Three-substage pipeline** (cheapest API first, most expensive last; each stage only queries survivors of the prior):

  | Stage | Source | Fields captured | Tickers touched | Per-call cost | ETA cold |
  |---|---|---|---|---|---|
  | **1a** `fast_info` | `yf.Ticker(sym).fast_info` | `market_cap, currency, exchange, three_month_average_volume, last_price` | ~10k (all SEC) | ~0.1s | ~100 min |
  | **1b** `.info` | `yf.Ticker(sym).info` | `totalRevenue` | ~1500 Stage-1a survivors (mcap+currency+exchange pass) | ~1.0s | ~25 min |
  | **1c** SEC submissions | `data.sec.gov/submissions/CIK{cik:010d}.json` | `sic, sic_description, n_10k, n_10q` | ~1400 Stage-1b survivors (revenue pass) | ~0.15s | ~4 min |

- **Checkpoints** (resume-safe — each stage writes its own):
  - `.universe_checkpoint.csv` (Stage 1a output)
  - `.universe_revenue_checkpoint.csv` (Stage 1b output)
  - `.universe_sec_checkpoint.csv` (Stage 1c output)
- **Output:** `universe_raw.csv` with full schema:
  ```
  symbol, cik, name, market_cap, currency, exchange,
  avg_dollar_volume_90d,       # three_month_average_volume × last_price
  annual_revenue,
  sic, sic_description, n_10k, n_10q
  ```
- Rate limit: 0.6s floor between HTTP calls (SEC fair-use + yfinance throttle); ~130 min cold total; warm restart < 1 min via checkpoints
- **`--no-resume` flag** to force fresh rebuild (for quarterly refresh)

**Already built** (current `universe_builder.py`): Stage 1a + Stage 1b + the investability filter. **Still to build**: Stage 1c (SEC submissions → sic, n_10k, n_10q) and the corresponding checkpoint + schema extension.

### Phase 1.1 — Prescreen (~0.25 day) — *pure-local filter, no HTTP*

**Goal**: trim the Phase 1.0 survivors down to a framework-compatible set. Because Phase 1.0 already captured every field we need, Phase 1.1 is **pure local logic over `universe_raw.csv`** — a single pass, well under 1 minute, re-runnable as many times as we want to retune the thresholds.

**Shipped behavior (2026-04-23, as in [`prescreen_rules.json`](prescreen_rules.json)):** six rules, no `target_size` cap. The current run produces **1,414 rows** in `universe_prescreened.csv`, and that same 1,414 flows through to `screener_results.csv` (see [README.md](README.md) file-structure section).

| # | Rule | Source field (captured in Phase 1.0) |
|---|---|---|
| 1 | `avg_dollar_volume_90d ≥ $5M` | Stage 1a: `three_month_average_volume × last_price` |
| 2 | `n_10q > 10` (strict; covers the `≥ 5` 10-K requirement implicitly for mature filers) | Stage 1c: SEC submissions count |
| 3 | `sic ∉ excluded_sic_ranges` | Stage 1c: SEC submissions SIC |
| 4 | `sic` not blank | Stage 1c |
| 5 | `market_cap ÷ annual_revenue ≤ 50` (`max_svr`) — cuts cash shells and pre-revenue biotech outliers | Stage 1a + 1b |
| 6 | Finance sector (SIC 6000-6999) capped at top 50 by annual revenue; optional dual-class dedup by CIK | Stages 1b + 1c |

Rules 5 and 6 are **additions** to the original three-axis spec — they emerged during the 1.1 implementation to kill cash shells and to stop dual-class share pairs (e.g. GOOG/GOOGL) from both consuming slots.

**No `target_size` cap.** The config does not carry a hard top-N; the original "top 500 by market cap after filters" was dropped once the rule set above brought the pass count down to ~1,400 on its own. The README and Layer 2 universe description treat this 1,414-row output as the canonical prescreen size; future retuning should edit `prescreen_rules.json` rather than reintroduce `target_size` without updating both docs.

**Excluded SIC sectors** (framework incompatible, as currently configured):
- Banks: 6020-6030
- REITs: 6798
- Investment funds / holding shells: 6199, 6722, **6726** (added), 6770

**Kept in universe** (user decision — archetype tagging in 1.3 contextualizes the verdict):
- Insurance: 6300-6411 — float accounting but major carriers (BRK, PGR, CB) are legitimate "leader" candidates
- Utilities: 4911-4939 — regulated margins but cleanly map to ARISTOCRAT archetype

**Implementation** (in `universe_builder.py`):
1. Load `prescreen_rules.json` (create with defaults on first run).
2. For each row in `universe_raw.csv`: evaluate rules 1-6 against captured fields.
3. Attach `prescreen_pass_reason` per row (`"pass"`, `"fail:liquidity"`, `"fail:filings"`, `"fail:sic=6020"`, `"fail:svr"`, `"fail:finance_cap"`, `"fail:dup_cik"`) for debugging.
4. Emit `universe_prescreened.csv` (same schema as `universe_raw.csv` + `prescreen_pass_reason`).

**Why this is a big win**: retuning a threshold (e.g. raising liquidity floor to $10M, or un-excluding SIC 6798) triggers only a local CSV re-read — no HTTP, no 2-hour rebuild. Phase 1.0 is the expensive gate; Phase 1.1 is a spreadsheet filter.

### Phase 1.2 — Batch SEC Fetch (~2 days)
- **Reuse existing:** `edgar_fetcher.py` — already has rate limit, retry, SIC lookup, SQLite cache, 90-day TTL
- Add a `--universe <csv_path>` flag that reads `universe_prescreened.csv` and calls `fetch_one` for each symbol
- Expect: 500 tickers × ~10s each ≈ 85 minutes cold-load
- Warm re-runs finish in seconds (90-day TTL)
- Existing `fundamentals.db` schema is sufficient (tickers, filings, facts tables)

### Phase 1.3 — Good Firm Screen + Sector Rank (~1 day)
- **Reuse existing:** `fundamental_metrics.py` (15 metrics + archetype classifier already shipped)
- **Reuse existing:** `fundamental_screener.py` (5 tests + 3 dealbreakers + verdict + SVR sector context)
- Sector ranking uses SIC 2-digit major group (already implemented as `_sector_key`)
- At 500 tickers nearly all sectors clear the `min_peers ≥ 3` guard (this was the ISRG bug at 85 tickers — self-healing at scale)
- Output: `screener_results.csv` (symbol, verdict, good_firm_score, archetype, sector_rank, all 15 metrics)

### Phase 1.4 — Leader Selection (~0.5 day)
- **New file:** `leader_selector.py`
- Input: `screener_results.csv`
- Selection rules (ordered):
  1. Include all `INDUSTRY_LEADER` tickers (expect ~30–50)
  2. Include top `POTENTIAL_LEADER` by good_firm_score until total = 100
  3. If slots remain: top `HIDDEN_GEM` per SIC-2 sector (1 per sector)
  4. Hard exclude: any ticker with `AVOID` verdict or any dealbreaker flag
- Output: `leaders.csv` (symbol, cik, name, sector, verdict, good_firm_score, archetype, selection_reason)
- Quarterly scheduler trigger in `api_server.py`: APScheduler cron Feb 15 / May 15 / Aug 15 / Nov 15 at 2 AM (post 10-Q season)

### Phase 1.5 — Layer 2 Handoff (~0.25 day)
- **Modify:** `finance_model_v2.py:109` `get_all_symbols()` — change source from `Tickers.csv` to `leaders.csv`; fall back to `Tickers.csv` if `leaders.csv` is missing or empty (first-run safety + rollback)
- **No other Layer 2 changes.** Scheduler, backtest, prediction code, TTL cache, three existing tabs all continue to work because they route through `get_all_symbols()`
- This is the single line of code that gates the whole pivot

### Phase 1.6 — Leader Detector UI (~1 day)
- **Modify:** `frontend/index.html` (extend only, no existing-tab edits)
- Add a 4th tab `Leader Detector` to the `.tabs` nav
- Tab contents:
  - Top bar: "Last rebuild: YYYY-MM-DD · N prescreened · K leaders selected · [Rebuild Now] [Download leaders.csv]"
  - Table of all ~500 prescreened tickers, sortable by rank, columns: Symbol, Name, Sector, Market Cap, Verdict, Firm Score, Archetype, Sector Rank, Selected (✓/—)
  - Filter chips: All / Selected Only / By Verdict / By Sector
  - Click symbol → reuses the inline detail card already built for Daily Report (no new card code)
- **Modify:** `api_server.py` — add 3 endpoints (additive only):
  - `GET /api/leaders` → current `leaders.csv` as JSON
  - `GET /api/universe` → full ranked 500 (`screener_results.csv`) as JSON
  - `POST /api/leaders/rebuild` → trigger pipeline in background (admin)

### Phase 1.7 — Metrics QA Pass (~1 day, post-1.0 blocker on Phase 1.5)
**Goal**: fix XBRL extraction bugs in `fundamental_metrics.py` surfaced by the Phase 1.3 quick-test before `leaders.csv` ships to Layer 2. These are *pre-existing* bugs that the static 85-ticker universe was masking — they only surface when ranking hundreds of tickers competitively.

**Known bugs from the 5-ticker quick-test** (2026-04-18, `quick_test_screener.csv`):
- **COST gross margin = 6%** (real value ~13%). Likely a misnamed XBRL tag — pulling `OperatingExpenses` or `SellingGeneralAndAdministrativeExpense` into the COGS numerator instead of `CostOfRevenue` — or a quarterly-vs-annual time-window mismatch.
- **ORCL free cash flow = negative** (real value ~$11B TTM). Likely either a sign flip in the CapEx reconciliation or a TTM window that incorporates an anomalous cloud-infra capex quarter.

**QA approach**:
1. Pick ~20 known-good large-caps spanning all archetypes: AAPL, MSFT, NVDA, V, MA, LLY, JNJ, COST, WMT, ORCL, XOM, CVX, JPM, HD, ADBE, NFLX, DIS, BA, GE, ISRG.
2. Manually extract each of the 15 metrics from each ticker's latest 10-K.
3. Diff against `fundamental_metrics.py` output; identify which specific XBRL tag extractors disagree.
4. Patch the offending extractors; add unit tests that lock in the golden values.
5. Re-run `python fundamental_screener.py --all --csv-out screener_results.csv`; verify the 20 golden tickers produce plausible verdicts.

**Deferred until after**: Phase 1.0 lands, so QA uses the real ~500-ticker screener output as regression signal rather than the 5-ticker quick-test (too small to surface sector-specific bugs).

**Gate**: Phase 1.5 (the `get_all_symbols()` swap) should NOT ship until Phase 1.7 reduces the "obvious-wrong verdict" rate on the top-100 leaders to ≤5%. Concrete pass criterion: the top-100 list must contain ≥15 S&P 100 members that any finance-savvy human would agree are "leaders", and must not contain any well-known underperformer tagged `INDUSTRY_LEADER`.

---

## Key Files at a Glance

| File | Status | Purpose |
|---|---|---|
| `universe_builder.py` | **NEW** | Phases 1.0 + 1.1: SEC ~10k → unified metadata gather → pure-local filter → 500 |
| `leader_selector.py` | **NEW** | Phase 1.4: pick ≤100 leaders from ranked 500 |
| `prescreen_rules.json` | **NEW** | Phase 1.1 config (liquidity floor, filing counts, excluded SIC ranges) |
| `.universe_checkpoint.csv` | **NEW (generated)** | Phase 1.0 Stage 1a checkpoint (fast_info results) |
| `.universe_revenue_checkpoint.csv` | **NEW (generated)** | Phase 1.0 Stage 1b checkpoint (.info revenue results) |
| `.universe_sec_checkpoint.csv` | **NEW (generated)** | Phase 1.0 Stage 1c checkpoint (SEC submissions: sic, n_10k, n_10q) |
| `universe_raw.csv` | **NEW (generated)** | Phase 1.0 output — rich per-ticker metadata (symbol, cik, name, market_cap, currency, exchange, avg_dollar_volume_90d, annual_revenue, sic, sic_description, n_10k, n_10q) |
| `universe_prescreened.csv` | **NEW (generated)** | Phase 1.1 output — ~500 rows (universe_raw.csv schema + prescreen_pass_reason) |
| `screener_results.csv` | **NEW (generated)** | Phase 1.3 output |
| `leaders.csv` | **NEW (generated)** | Phase 1.4 output — feeds Layer 2 |
| `edgar_fetcher.py` | **REUSED** | Already has SEC ~10k loader, SIC lookup, 90-day TTL |
| `fundamental_metrics.py` | **REUSED** | 15 metrics + archetype classifier already shipped |
| `fundamental_screener.py` | **REUSED** | Verdict + sector rank already shipped |
| `fundamentals.db` | **REUSED** | SQLite schema already sufficient |
| `api_server.py` | Modified (additive, +3 endpoints + scheduler cron) | `/api/leaders`, `/api/universe`, `/api/leaders/rebuild` |
| `frontend/index.html` | Modified (additive, +1 tab) | Leader Detector tab |
| `finance_model_v2.py` | Modified (1 line, Phase 1.5) | `get_all_symbols()` reads `leaders.csv` with fallback |
| Scheduler daily-report cron (`_run_dual_report`) | **UNTOUCHED** | Continues to run at 4:05 PM EST |
| Three existing tabs | **UNTOUCHED** | |
| Backtest pipeline + 7-day TTL | **UNTOUCHED** | |
| Ticker Lookup fundamental verdict card | **UNTOUCHED** | Reused by Leader Detector tab |

---

## Dependencies
- All Python libs already installed (`yfinance`, `apscheduler`, `pandas`, `sqlite3`, stdlib `csv`/`json`/`urllib`)
- No new dependencies required

---

## Verification

### Layer 1 end-to-end
1. `python universe_builder.py --build` → `universe_raw.csv` (~1500 rows) and `universe_prescreened.csv` (~500 rows) created
2. `python edgar_fetcher.py --universe universe_prescreened.csv` → `fundamentals.db` populated for 500 tickers (run overnight)
3. `python fundamental_screener.py --all` → `screener_results.csv` with verdicts + sector ranks
4. `python leader_selector.py --build` → `leaders.csv` with ≤100 tickers
5. `curl localhost:8000/api/leaders` → JSON of leaders.csv
6. `curl localhost:8000/api/universe` → JSON of ranked 500
7. Open Leader Detector tab → table renders 500 rows, selected leaders visually distinguished, click symbol opens inline detail card

### Layer 2 regression (MUST pass, gate for shipping)
8. Restart API server after Phase 1.5 → Daily Report tab populates from leaders.csv (was Tickers.csv), same column layout as today
9. Ticker Lookup tab → picking `ISRG` still shows verdict card exactly as before (card rendering code unchanged)
10. Strategy Lab tab → backtest library runs over leader universe; 7-day TTL cache still works; CACHED pill still appears on repeat runs
11. Scheduled 4:05 PM EST daily report cron runs over leader universe without error

### Rollback safety
- If `leaders.csv` is missing or empty → `get_all_symbols()` falls back to `Tickers.csv` (Phase 1.5 safeguard)
- Delete `leaders.csv` → system returns to pre-pivot behavior instantly; no code rollback needed

---

## Out of Scope / Deferred

- **Path B per-archetype rubrics** (swapping 2 of 5 tests for ARISTOCRAT, GROWTH, etc.) — deferred until after Layer 1 is reviewed and working in production.
- **Banks / REITs / investment-fund shells** — excluded from universe by SIC prescreen in Phase 1.1. Would require a separate framework (net interest margin, FFO, NAV) to analyze.
- **International tickers** — universe is US-only (exchange reject-list + USD currency).
- **Intra-quarter auto-updates** (e.g. rebalance on 8-K filings) — quarterly is the cadence.
- **Historical leader list snapshots / point-in-time backtesting** — each rebuild overwrites `leaders.csv`; no history kept yet.
- **Insider-buying signal, earnings-call NLP, retention metrics (NRR/GRR)** — all remain outside XBRL scope.
