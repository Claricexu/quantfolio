# Quantfolio — Audit findings

Read-only audit conducted 2026-04-21 by a three-teammate review (wright / skipper / sophia), each with an independent pass followed by a cross-check of a peer's findings. Severity reflects the consolidated view after cross-check (several items were escalated or de-escalated — noted inline).

No trading strategies, statistical soundness, or financial meaning were evaluated. Code, data flow, and UX only.

---

## Summary

Quantfolio's four-tab / two-layer story is *architecturally* coherent but *operationally* fractured. The biggest structural issue is that three UI surfaces that should read the same fundamental-verdict data read it three different ways, so the same ticker can appear as `LEADER`, `INSUFFICIENT_DATA`, and `—` across tabs in one session — a trust-destroying inconsistency for a finance tool. The Lite/Pro distinction is visible and consistent inside each tab, but the pipeline around it is fragile: three independently-drifted backtest implementations produce different Sharpe ratios for the same ticker, the dashboard's High-Confidence SELL section does not apply the Full-Signal filter the email path does, and a 60-second frontend fetch timeout silently aborts long-running operations while the server is still working fine. Documentation is more aspirational than enforced: `DEVELOPMENT.md §8`'s test suite does not exist, `§12`'s pitfalls are documented but only one is guarded in code, `§6`'s cache table does not match the actual filenames or TTL behavior, and `USER_GUIDE.md` Parts 4 and 6 disagree with the shipping UI on column names, refresh timing, and filter semantics. `requirements.txt` is 100% `>=` with no pinned versions — reproducibility of backtests, the product's core claim, depends on whatever pip happens to resolve today. The verdict-card UI (the focus of the latest commit) is actually the *cleanest* surface in the app: one `buildVerdictCard()` function serves three entry points with consistent rendering, and the `INSUFFICIENT_DATA` sentinel branches gracefully — that part of the recent work is solid.

---

## Critical issues

### C-1. Screener endpoints read only `Tickers.csv`, not `leaders.csv` — "Three-Tab Gaslight"
- **Owner:** wright (cross-checked by sophia for UX impact)
- **Location:** `api_server.py:1144` → `fundamental_screener.py:363-381` → `edgar_fetcher.py:393-401` (loader) and `api_server.py:1233-1242` (`/api/screener/{symbol}`)
- **Description:** `/api/screener` and `/api/screener/{symbol}` compute the screen over the 85-ticker manual watchlist only. A ticker that is in `leaders.csv` but not `Tickers.csv` will render as `LEADER` (green) in the Leader Detector table, `INSUFFICIENT_DATA` in Ticker Lookup's verdict card, `—` in the Daily Report Firm Score column, and "No Fundamental Data" in the Leader Detector click-through panel — all in one session, with no explanation. `screener_results.csv` on disk has the real verdict; nothing reads it here. The `/api/screener/{symbol}` endpoint additionally *fabricates* an `INSUFFICIENT_DATA` response with a misleading hint ("may be an ETF/ADR") for any symbol it can't find locally.
- **Suggested fix direction:** have both endpoints prefer `screener_results.csv` by symbol (the Layer 1 output that already feeds `/api/universe?source=screener`), falling through to the Tickers.csv recomputation only if the CSV is missing. Single source of truth across all four tabs.

### C-2. Dashboard High-Confidence SELL section does not apply the Full-Signal filter documented in `USER_GUIDE` Part 4
- **Owner:** sophia (cross-checked by skipper — escalated to data-integrity bug)
- **Location:** `USER_GUIDE.md` Part 4 vs `frontend/index.html:1117` (`renderReport`) and `api_server.py:128` (email path — source of truth)
- **Description:** The guide promises "SELLs on stocks whose best strategy is Buy-Only or Buy & Hold are filtered out." That filter lives only in `_send_alert_email()` at `api_server.py:128`. The dashboard's Table of high-conf SELL rows happily shows SELL on a Buy-Only stock. This is the dashboard actively emitting a trade signal the validated strategy has no SELL rule for.
- **Suggested fix direction:** move the filter server-side — expose `best_strategy_key` as a field in the `/api/report` response (it is already looked up for the email path) and have the frontend row renderer downgrade any SELL whose `best_strategy_key ∉ {lite_full, pro_full}` to HOLD. Keeps email and dashboard on one rule.

### C-3. Three backtest implementations have silently diverged — same ticker, different Sharpe
- **Owner:** skipper (escalated from HIGH → CRITICAL by wright during cross-check)
- **Location:** `finance_model_v2.py:484-502` (inline in `predict_ticker`), `:766-801` (`backtest_symbol`), `:854-902` (`backtest_multi_strategy`)
- **Description:** Same finite-state machine encoded three times with different guards and different ensemble builders. `predict_ticker`'s inline loop omits the `MIN_ZSCORE_SAMPLES=20` floor that the other two enforce. `backtest_symbol` uses `build_stacking_ensemble` (slow, OOF-trained); `backtest_multi_strategy` uses `build_stacking_ensemble_fast` (val-set MAE). The CLI backtest and the dashboard's `/api/backtest-chart/{symbol}` therefore report different numbers for the same ticker. For a product whose core value is ranking strategies by Sharpe, this is a correctness failure, not drift.
- **Suggested fix direction:** extract a single `BacktestEngine(prices, predictions, strategy_fn) → (portfolio, stats)` helper. Delete the inline loop in `predict_ticker` and replace with one engine call. Pick one ensemble builder and document the speed/accuracy tradeoff on the chosen function.

### C-4. SEC EDGAR HTTP path has no retry, no `Retry-After` handling, and can burst past the documented 10 req/sec
- **Owner:** skipper (wright: collapses with C-5 into "no shared HTTP client")
- **Location:** `edgar_fetcher.py:178-184` (`http_get_json` — one-shot `urlopen`), `:272-285` (`fetch_one` aborts ticker on 429), `:366-387` (sleep is per-ticker, not per-request; facts + submissions can fire inside one sleep window)
- **Description:** A single 429 permanently drops that ticker from the run — on a cold ~500-symbol universe rebuild (3.5 h per `DEVELOPMENT.md`) this is the dominant failure mode. `DEVELOPMENT.md §7` promises 10 req/sec discipline; measured bursts reach ~16 req/sec because facts + submissions calls share one outer sleep.
- **Suggested fix direction:** wrap `http_get_json` in exponential backoff that honors `Retry-After` and a token-bucket limiter sized to the documented budget. Fixing this together with C-5 under one shared client is cleaner than two local patches.

### C-5. yfinance 429 / empty-response is not retried — ticker silently dropped
- **Owner:** skipper (wright: same root cause as C-4)
- **Location:** `finance_model_v2.py:239-247` (`_download_batch`), `:258-266` (`fetch_stock_data`, `if sdf.empty: continue`)
- **Description:** `yf.download` returns an empty DataFrame on rate-limit — the retry loop catches exceptions, not empty returns. The 4:05 PM scheduled scan across 174 tickers will silently zero-out a subset on a Yahoo flap with only a generic `[scan] sym: …` log line. The user sees `"error": "No data available"` and assumes the ticker is broken.
- **Suggested fix direction:** treat empty-but-expected as a retryable state inside `_download_batch`; surface a rate-limit warning count from `fetch_stock_data`. Ideally under the same shared HTTP client as C-4.

### C-6. `fundamentals.db` (782 MB SQLite) is not WAL, has no `busy_timeout` — concurrent refresh + screener requests will lock
- **Owner:** skipper (wright: symptom of "no storage layer")
- **Location:** `edgar_fetcher.py:168-173` (`get_db`)
- **Description:** `sqlite3.connect(str(DB_PATH))` with no `timeout=`, no `PRAGMA journal_mode=WAL`. `_edgar_refresh_worker` holds a write connection for ~85 min during a refresh while `/api/screener` opens short-lived readers. A user hitting the screener during a refresh will see `SQLITE_BUSY` errors with no retry path.
- **Suggested fix direction:** one-line fix — set `PRAGMA journal_mode=WAL` and pass `timeout=30.0` inside `get_db()`. Readers and writer coexist cleanly in WAL.

### C-7. `requirements.txt` is 100% unpinned — reproducibility of backtests is not guaranteed
- **Owner:** skipper (wright: direct threat to product claim)
- **Location:** `requirements.txt:1-12`
- **Description:** Every line is `>=` only. `yfinance>=0.2.36` spans breaking `.info` shape changes; `xgboost>=2.0` spans `early_stopping_rounds` constructor-arg migration; `pandas>=2.0` spans MultiIndex column handling that `finance_model_v2.py:262` relies on. A user re-running a backtest tomorrow may get a different Sharpe from today with no code change, and have no way to prove the math is stable.
- **Suggested fix direction:** `pip freeze > requirements.lock`, check it in, pin `requirements.txt` to exact versions that have been tested. Keep `>=` only in a separate `requirements.in` for pip-compile if desired.

### C-8. Frontend `fetchTimeout` default (60 s) silently aborts long-running polls
- **Owner:** sophia (escalated by skipper to reliability-critical)
- **Location:** `frontend/index.html:394` default, applied to `loadReport:1005`, `pollReport:1040`, `pollBacktest:1368`
- **Description:** First-time report generation and batch backtests run for minutes. The per-call `AbortController` fires at 60 s while the server is still happily computing — user sees a silent stall followed by "Connection error" and retries, compounding server load. Server-side everything is fine; the UI is lying about the state.
- **Suggested fix direction:** split into `fetchWithTimeout` (short endpoints) vs `fetchNoTimeout` (polls); or pass a sentinel `timeoutMs: 0` to the long-poll call sites and skip the timer. Surface a visible "still working, last checked 00:42 ago" status line instead of silent retries.

### C-9. `predict_ticker` silently predicts on stale features when today's row has NaN
- **Owner:** skipper
- **Location:** `finance_model_v2.py:460-522` — `latest_row = df.iloc[-1:]`, then `if np.isnan(lf).any(): lf = aX[-1:].copy()` at `:522`
- **Description:** When any V3 feature is NaN on today's row (e.g. ROC_60d for a young IPO, one missing Volume day), the prediction quietly falls back to the previous complete row's feature vector — producing a confident-looking prediction from stale inputs with no warning in the response. This is `DEVELOPMENT.md §12`'s "stale data" pitfall, half-guarded (won't return 0%) but not flagged.
- **Suggested fix direction:** on fallback, attach `warnings: ["stale_features_used"]` to the response and surface a muted banner in the UI. Promotes silent degradation to visible degradation.

### C-10. `start_dashboard.bat` dependency check is one-import-deep — partial installs boot and crash mid-request
- **Owner:** sophia (escalated by skipper to deployment bug)
- **Location:** `start_dashboard.bat:39` (`python -c "import fastapi"`) then `pip install -r requirements.txt` gated on that
- **Description:** If a user has `fastapi` but not `lightgbm`/`xgboost`/`yfinance`/`apscheduler`, the check passes and the server crashes at import time with a Python traceback. A first-time user sees a red stack trace and cannot distinguish "missing dep" from "real bug."
- **Suggested fix direction:** expand the import check to the full top-level set (`import fastapi, uvicorn, pandas, numpy, sklearn, yfinance, xgboost, lightgbm, apscheduler`) — any failure triggers `pip install -r requirements.txt` and a clear message naming the missing package.

### C-11. Daily Report timing claim is off by 10–20× between guide and UI
- **Owner:** sophia
- **Location:** `USER_GUIDE.md` Part 4 ("2-5 minutes", Part 11 "5-10 minutes") vs `frontend/index.html:1014, 1024` ("40–90 minutes")
- **Description:** A first-time user reading the guide panics when the scan runs ≥10 min. A user reading the UI assumes the tool is broken. One of the two numbers is wrong; both cannot be right.
- **Suggested fix direction:** measure one fresh scan end-to-end, pick a single honest number, update both the guide and the UI banner. Also switch the button label from `Generate Report` to `Refresh Report` once data is loaded.

### C-12. Daily Report status banner can show "Generated: Invalid Date"
- **Owner:** wright (escalated by sophia — first-run onboarding impact)
- **Location:** `api_server.py:605` (filename fallback for `generated_at`) → `frontend/index.html:1074` (`new Date(json.generated_at)`)
- **Description:** If a cached `dual_report_*.json` has no `summary.generated_at`, the code falls back to the filename string `dual_report_20260421_1605.json`, which `datetime.fromisoformat` rejects. The 22h cache fast-path is silently disabled; the frontend renders `Invalid Date`. On a finance dashboard, "when was this computed" is the single most load-bearing metadata field — seeing "Invalid Date" at the top of the page signals the whole report might be unreliable.
- **Suggested fix direction:** if `summary.generated_at` is missing, derive an ISO timestamp from the file's `mtime`, not the filename. Never produce a non-ISO string for that field.

---

## High-priority

### H-1. Daily Report Firm Score column and Leader Detector table read from different code paths — same ticker can disagree across tabs
- **Owner:** wright
- **Location:** `frontend/index.html:984` (Daily Report loads `/api/screener`) vs `:1991` (Leader Detector loads `/api/universe?source=screener`)
- **Description:** One is an in-memory recompute over 85 tickers; the other is the 1,414-row `screener_results.csv`. If the two snapshots are from different `fundamentals.db` states (normal after a rebuild), the same ticker shows different verdict / score on different tabs within one session. Power users doing cross-checks — the ones whose trust matters most — will spot this.
- **Suggested fix direction:** serve both from the same loader (always read `screener_results.csv` as truth, or always recompute). Until then, add a small "as of HH:MM" chip next to each verdict surface so the divergence is at least legible.

### H-2. `/api/report` serves reports loaded from disk with no age check
- **Owner:** skipper (wright: borderline CRITICAL — reputational)
- **Location:** `api_server.py:444-474` (`_get_cached_compare_result`), `:592-608` (`_load_latest_report_from_disk`)
- **Description:** `_load_latest_report_from_disk` at startup loads the newest `dual_report_*.json` with no age gate. After a long-weekend reboot the user sees Friday's report served as "today," since the only TTL check is a lazy 22h window that can pass or fail depending on exact timing.
- **Suggested fix direction:** reject loaded-from-disk reports older than 22h at load time, log the age on startup, and add a `stale: true` flag to the response body if serving an aged report.

### H-3. Pro (v3) model can be `null` when LightGBM is not installed — no UI indication
- **Owner:** wright (escalated by sophia — product-integrity)
- **Location:** `finance_model_v2.py:599` (`r_v3 = ... if HAS_LGBM else None`), `api_server.py:702` (HAS_LGBM branch), `frontend/index.html:921` (silent `"Not available"`)
- **Description:** The product's headline differentiation is Lite-vs-Pro comparison. When LightGBM is absent (a plausible fresh-install outcome given unpinned deps, C-7), the Pro column silently shows "Not available" with no global banner, no "install LightGBM to enable Pro" instruction, no README note that v3 can be null. A user will assume Pro is broken or that their own setup is the problem.
- **Suggested fix direction:** on startup, detect `HAS_LGBM=False` and surface a persistent banner in the dashboard header ("Pro model unavailable — install `lightgbm` to enable"); add a one-liner to the README API contract noting `v3` may be null.

### H-4. `/api/screener/refresh` is an orphan endpoint — and the `INSUFFICIENT_DATA` hint tells users to run a refresh they can't invoke
- **Owner:** wright (escalated by sophia — combined with C-1's hint text)
- **Location:** `api_server.py:1246-1285` (defined), `frontend/index.html` (no caller); hint string appears in `buildVerdictCard` rendering
- **Description:** The endpoint exists. The frontend never calls it. Meanwhile the `INSUFFICIENT_DATA` hint tells users to "run SEC refresh" — and there is no button anywhere in the UI that does that. The tool gives a call-to-action it cannot fulfill.
- **Suggested fix direction:** either wire a "Refresh SEC data" button into the Leader Detector tab (gated on admin intent), or remove the endpoint and rewrite the hint to reference something the user can actually do.

### H-5. API accepts `weight_rf`, `weight_xgb`, `rolling_window` and silently drops them into `**kwargs` with no consumer
- **Owner:** skipper (wright: symptom of "no parameter contract")
- **Location:** `api_server.py:407-433` (`api_predict` normalizes weights), `finance_model_v2.py:448` (`predict_ticker(..., **kwargs)` — none of these names appear anywhere in the file)
- **Description:** FastAPI accepts, normalizes, and passes through three parameters that `predict_ticker` never reads. Any future frontend slider wired to these will be a no-op. The Lite ensemble weights are hardcoded `0.8 / 0.2` at `finance_model_v2.py:388`.
- **Suggested fix direction:** Pydantic request model with fields that map 1:1 to `predict_ticker` kwargs; delete `**kwargs` from `predict_ticker` so unknown params raise `TypeError` at the boundary. Prevents a class of silent-failure bugs going forward.

### H-6. `_load_library_summary` reads ~174 JSON files on every request
- **Owner:** skipper
- **Location:** `api_server.py:930-976` (`_load_library_summary`) called via `_get_best_strategy_map` from `/api/predict`, `/api/predict-compare`, `/api/report`, `/api/backtest-library`, and email build
- **Description:** ~9 MB of disk I/O and JSON parsing per request, every request. On a fast SSD ~200 ms of latency; on HDD or under load, worse. Not cached.
- **Suggested fix direction:** process-lifetime memoization keyed on the max `mtime` across the cache directory. Invalidate on any file change.

### H-7. No accessibility attributes anywhere — color-only state for BUY/SELL/HOLD
- **Owner:** sophia
- **Location:** `frontend/index.html` (zero `aria-*`, `role`, `alt` attributes)
- **Description:** Tabs are `<button>`s with no `role="tablist"` / `aria-selected`. Sortable `<th>`s have no `aria-sort`. Signal pills distinguish BUY/SELL/HOLD by color only — a user with red-green color blindness sees three identically-shaped pills differentiated only by the text inside.
- **Suggested fix direction:** add `role` / `aria-selected` to tabs, `aria-sort` to sortable headers, and a non-color affordance (icon or shape) to signal pills and confidence badges.

### H-8. Dead model-toggle code (`runPredict`, `setModel`, `#modelToggle`) still referenced in README
- **Owner:** sophia
- **Location:** `frontend/index.html:708-720` and `README.md` Dashboard Tabs section
- **Description:** The functions exist but the DOM node `#modelToggle` is not rendered anywhere. README still advertises a "Predict single-model or Compare Both" choice that the UI does not expose. A reader of the README expects a toggle that does not exist.
- **Suggested fix direction:** delete the three functions and the README line. If the toggle is intended to return, put a TODO with a date.

### H-9. Ticker Lookup verdict-card fetch fails silently
- **Owner:** sophia (skipper: observability bug)
- **Location:** `frontend/index.html:859` — `fetchTimeout('/api/screener/' + sym, 30000).catch(() => {})`
- **Description:** The second fetch that appends the fundamental verdict card has an empty catch. A hung or slow screener produces a prediction card with no verdict card at all — the user doesn't know anything is missing because the guide never tells them to expect one. A real outage is indistinguishable from normal behavior.
- **Suggested fix direction:** render a subtle "Fundamental verdict loading…" placeholder; on error show a compact "Fundamental data unavailable — retry" row instead of silent omission.

### H-10. `INSUFFICIENT_DATA` hint is wrong for known-taxonomy-gap filers (CRWD / VLO / APA / FSLY)
- **Owner:** sophia (skipper: correctness bug — backend should emit the reason)
- **Location:** hint text in `buildVerdictCard`; classifier lives in `fundamental_screener.py:287`
- **Description:** The generic hint reads "No SEC data (ETF/ADR, or SEC refresh not yet run)" — but CRWD/VLO/APA/FSLY *do* have SEC data, just with extension-taxonomy gaps the XBRL parser cannot resolve. A user looking up CRWD in Ticker Lookup is told their leader pick has no SEC data. Directly contradicts the project-memory note on Cat-A gaps.
- **Suggested fix direction:** emit a `reason` enum from the backend (`NO_SEC_FILINGS` vs `TAXONOMY_GAP` vs `INSUFFICIENT_HISTORY`). The backend knows which of these applies; the frontend just renders `row.reason_text`.

### H-11. First-install empty states across Daily Report / Strategy Lab / Leader Detector are unhelpful
- **Owner:** sophia
- **Location:** `frontend/index.html:1014` (Daily Report), `:1558` (Strategy Lab), `:2036` (Leader Detector)
- **Description:** Daily Report empty state is a single banner with no skeleton. Strategy Lab empty state has no time estimate for a 1–3 hour batch. Leader Detector empty state shows raw CLI commands (`python universe_builder.py --build`) — but the USER_GUIDE installation path targets non-developer users who double-click `start_dashboard.bat`.
- **Suggested fix direction:** skeleton layouts for each tab; explicit time costs on every long-running button; relegate CLI fallback to an expandable "Developer: manual rebuild" detail on the Leader Detector tab.

### H-12. Native `window.confirm()` is the only guard on a 3.5-hour destructive rebuild
- **Owner:** sophia
- **Location:** `frontend/index.html:2330` (`rebuildLeaders`)
- **Description:** Native confirms autodismiss easily and are OS-styled. A user who clicks through without reading starts a multi-hour SEC EDGAR job with no easy abort and no inline modal showing what's about to happen.
- **Suggested fix direction:** inline modal showing cold-vs-warm estimate, last-rebuild timestamp, and a required typed token (`REBUILD`) or checkbox to enable confirm.

### H-13. Strategy Lab batch has no cancel button and no ETA
- **Owner:** sophia
- **Location:** `frontend/index.html:1830` (`runBatchBacktest`), `:1863` (`pollBatchStatus`, `:1887` banner)
- **Description:** A user who starts a 3-hour batch and realizes they need to add a ticker first must kill the server. The polling banner shows `X/Y` but no wall-clock remaining estimate. Cancel requires a backend flag the worker loop reads — this is a design decision before a code change.
- **Suggested fix direction:** decide cancel semantics first (kill vs cooperative). Ship ETA (elapsed / completed × remaining) as a same-day cosmetic win regardless.

### H-14. `start_dashboard.bat` assumes miniconda and opens browser before server binds
- **Owner:** sophia
- **Location:** `start_dashboard.bat:16, 51, 53`
- **Description:** Activation path hard-codes `%USERPROFILE%\miniconda3\condabin\activate.bat`, but `USER_GUIDE` Part 1 tells users to install standard Python from python.org. Silent activate failure → misleading "open Anaconda Prompt manually" error. Separately, the browser opens after a fixed 3-second `timeout`, regardless of whether the server has bound port 8000 — on a slow first run users see "site can't be reached" and may give up.
- **Suggested fix direction:** detect conda-absent case and print a standard-Python message; poll `localhost:8000` before launching the browser (up to ~30 s).

### H-15. Library rows clickable but no visual affordance
- **Owner:** sophia
- **Location:** `frontend/index.html:1750` (`viewLibraryChart` onclick on `<tr>`)
- **Description:** Equity-curve chart is hidden behind undiscoverable interaction — no `cursor:pointer`, no hint row.
- **Suggested fix direction:** one CSS rule plus one micro-copy line above the table.

### H-16. Leader Detector filter chip counts only populated for SEL; Verdict and Archetype chips have no `(n)`
- **Owner:** sophia
- **Location:** `frontend/index.html` — `updateFilterChipCounts` populates SEL only; VERDICT / ARCHETYPE chips show `All / Leader / Gem / Watch / Avoid` with no count
- **Description:** User clicks blindly. Every piece of data needed is already on every row.
- **Suggested fix direction:** extend the chip-count computation to iterate all three facets.

### H-17. Native `alert()` on "no leaders to download" + Download CSV button always enabled
- **Owner:** sophia
- **Location:** `frontend/index.html:2449` (`downloadLeadersCsv`)
- **Description:** Empty-selection path uses `alert()`. Button is visible even with zero rows.
- **Suggested fix direction:** disable the button when `_leadersSelected.size === 0`; tooltip explains why.

---

## Nice-to-have

### N-1. Dead `Volatility_20d` feature column
- **Owner:** skipper
- **Location:** `finance_model_v2.py:314`, `:374` (`df['Volatility_20d']=df['RVol_20d']`)
- **Description:** Both feature-engineering functions create this column; nothing downstream reads it. Confuses anyone adding new features.
- **Suggested fix direction:** delete the two assignments.

### N-2. `compute_metrics(symbol, sector_context=None)` — `sector_context` is dead parameter
- **Owner:** skipper
- **Location:** `fundamental_metrics.py:380-461`, caller `fundamental_screener.py:373`
- **Description:** Caller never passes `sector_context`. Ranking is injected post-hoc by `apply_sector_context`.
- **Suggested fix direction:** remove the parameter or refactor to actually use it.

### N-3. `SYMBOL_UNIVERSE` frozen at import, stale after leader rebuild
- **Owner:** skipper
- **Location:** `finance_model_v2.py:189`, `api_server.py:660`
- **Description:** `/api/symbols` returns `categories` (stale) and `all` (fresh) that will disagree after a quarterly rebuild until server restart.
- **Suggested fix direction:** make it a function call, not a frozen dict.

### N-4. `_cache_fresh` swallows all exceptions — corrupt CSVs silently re-download
- **Owner:** skipper
- **Location:** `finance_model_v2.py:237`
- **Description:** `except Exception` catches `ParserError`, `KeyError`, anything. No log.
- **Suggested fix direction:** log the exception type / message before returning False.

### N-5. `_fetch_svr` swallows yfinance schema changes silently
- **Owner:** skipper
- **Location:** `finance_model_v2.py:292-293` and `fundamental_metrics.py:332-342`, `:363-374`
- **Description:** yfinance renaming `sector` / `industry` / `marketCap` in a future release turns into `svr=None` for every ticker with no warning. High-risk combination with C-7 (unpinned deps).
- **Suggested fix direction:** log the exception at warn level on first occurrence per run.

### N-6. Backtest chart v2/v3 length-mismatch trims the wrong `buyhold` in edge cases
- **Owner:** wright
- **Location:** `api_server.py:732-740`
- **Description:** Trims both `buyhold` arrays but only the primary is surfaced. A future refactor emitting both will be silently inconsistent.
- **Suggested fix direction:** drop the non-primary trim; add a comment that only primary's buyhold flows to the response.

### N-7. `_load_latest_scan_from_disk` sorts alphabetically across version-prefixed files
- **Owner:** wright
- **Location:** `api_server.py:358-375`, writers at `:297-298`
- **Description:** `daily_scan_auto_...` sorts before `daily_scan_v3_...`. Rarely triggered today; a trap the day version prefix changes.
- **Suggested fix direction:** sort by `mtime` or put ISO date first in the filename.

### N-8. `/api/movers` + `daily_scan_*.csv` write path is dead — `/api/report` is what the UI uses
- **Owner:** wright
- **Location:** `api_server.py:290-313` vs `:592-608`
- **Description:** Two parallel scan paths write different files and maintain different caches; frontend uses only `/api/report`.
- **Suggested fix direction:** document as legacy / remove.

### N-9. `_is_dealbreaker` checks a flag (`flag_spac_or_microcap`) the screener never sets
- **Owner:** wright
- **Location:** `leader_selector.py:69-86` vs `fundamental_screener.py:214-216`
- **Description:** Dead defense-in-depth.
- **Suggested fix direction:** either remove or document as forward-compat only.

### N-10. `target_size` default 100 hardcoded in three places
- **Owner:** wright
- **Location:** `leader_selector.py:56`, `README.md:33`, `api_server.py:1360`
- **Description:** Future drift point if the default changes.
- **Suggested fix direction:** read from a config file referenced by the README, or expose on the rebuild endpoint as a query param.

### N-11. Footer reads "78+ tickers" where every other surface says 174
- **Owner:** sophia
- **Location:** `frontend/index.html:361`
- **Suggested fix direction:** compute from `/api/symbols`, or update the string.

### N-12. Leader Detector rebuild error state shows raw stderr with no retry button
- **Owner:** sophia
- **Location:** `frontend/index.html` rebuild-status render path
- **Suggested fix direction:** inline retry button + "copy error" affordance.

### N-13. `INSUFFICIENT_DATA` em-dash in the Leader Detector verdict column is invisible
- **Owner:** sophia
- **Location:** `verdictShort`, `verdictColor` at `frontend/index.html:457`, `:500`
- **Description:** A grey em-dash pill is indistinguishable from "not evaluated" or "broken."
- **Suggested fix direction:** render as a visible "N/A" pill with a tooltip.

### N-14. Legacy 5-verdict names (`INDUSTRY_LEADER`, `HIDDEN_GEM`, `POTENTIAL_LEADER`) silently mapped in `verdictColor`/`verdictLabel`
- **Owner:** sophia
- **Location:** `frontend/index.html:457-505`
- **Suggested fix direction:** dev-only `console.warn` when a legacy verdict comes through, so a regression surfaces loudly.

### N-15. No automated test suite — `tests/` directory does not exist
- **Owner:** skipper
- **Location:** repo root (`tests/**` Glob → empty)
- **Description:** `DEVELOPMENT.md §8` calls out an "anchor regression test" as "the single most important regression barrier." It is not implemented.
- **Suggested fix direction:** write `tests/test_screener_verdicts.py` with the 10 anchor tickers (KO/JNJ/PG/WMT/MCD/NVDA/MSFT/META/CRWD/NOW) asserting expected verdicts. One file unlocks CI coverage on the most-drift-prone rubric.

---

## Doc-vs-code mismatches

One-liner per gap. All of these will bite a new teammate or a user returning to the tool after a few weeks.

- **`DEVELOPMENT.md §6` caching table** — lists `report_cache.json` with 22h TTL; actual filename is `dual_report_<YYYYMMDD_HHMM>.json` and `/api/report` enforces no TTL (only the lookup fast-path does). Silent on the 6h `/api/screener` TTL. **[wright]**
- **`DEVELOPMENT.md §7`** — promises "hard 10 req/sec" SEC discipline; observed bursts reach ~16 req/sec because facts + submissions calls share one outer sleep (`edgar_fetcher.py:366-387`). **[skipper]**
- **`DEVELOPMENT.md §8`** — lists four high-leverage test files; none exist. **[skipper]**
- **`DEVELOPMENT.md §12`** — six "Common pitfalls" documented; only one (`n_jobs=1` on RandomForest) is actually guarded in code. Others (stale features, silent email-alert failure, stale batch cache) are advice only. **[skipper]**
- **`TWO_LAYER_ARCHITECTURE_PLAN.md` Phase 1.1** — lists a 3-axis prescreen with `target_size: 500`; shipped code has 6 rules in `prescreen_rules.json` and no cap (`universe_builder.py:684`). README says 1,414 rows. **[wright]**
- **`README.md` API Endpoints block** — does not note that `v3` can be `null` when LightGBM is absent. **[wright]**
- **`README.md` Dashboard Tabs** — still advertises a "Predict (single model) or Compare Both" toggle; the UI does not expose it (`#modelToggle` is not rendered). **[sophia]**
- **`USER_GUIDE.md` Part 3 (Ticker Lookup)** — says SVR color hint reads "Overvalued"; UI text is "Expensive" (`frontend/index.html:432`). **[sophia]**
- **`USER_GUIDE.md` Part 3 (Ticker Lookup)** — implies a unified "Predicted price (next day)" headline on the compare card; the compare card has per-model columns with no combined headline. **[sophia]**
- **`USER_GUIDE.md` Part 4 (Daily Report)** — "SELLs on stocks whose best strategy is Buy-Only or Buy & Hold are filtered out." Dashboard does not apply this filter — only the email path does (`api_server.py:128`). See **C-2**. **[sophia]**
- **`USER_GUIDE.md` Part 4 (Daily Report)** — "2-5 minutes" refresh (Part 4), "5-10 minutes" (Part 11); UI says "40–90 minutes." See **C-11**. **[sophia]**
- **`USER_GUIDE.md` Part 4 table of columns** — lists 7 columns; UI has 10 (Lite Sig / Pro Sig / Firm Score are missing from the guide) and names differ (`Lite %` / `Pro %` vs `Lite Chg` / `Pro Chg`). **[sophia]**
- **`USER_GUIDE.md` Part 5 (Strategy Lab)** — "polls every 3-5 seconds"; code has two fixed rates (5000 ms for batch poll, 3000 ms for chart poll). **[sophia]**
- **`USER_GUIDE.md` Part 6 (Leader Detector)** — calls the column "Selected"; UI header is `SEL`. **[sophia]**
- **`USER_GUIDE.md` Part 6 (Leader Detector)** — says the Sector column shows "SIC 2-digit industry code"; UI shows a broad bucket (Technology, Health Care, …) via `broadSector()`. **[sophia]**
