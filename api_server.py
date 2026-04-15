"""
Quantfolio API Server
==============================
FastAPI server that wraps the Lite and Pro ML models for real-time predictions.

Endpoints:
  GET  /api/predict/{symbol}   — single-ticker prediction with SVR
  GET  /api/movers             — daily scan of all symbols, sorted by % change
  GET  /api/symbols            — list available symbol universe
  GET  /                       — serves the React dashboard (index.html)

Setup:
  pip install fastapi uvicorn apscheduler
  (plus all finance_model_v2 deps — see requirements.txt)

Run:
  python api_server.py
  → opens http://localhost:8000
"""

import os
import json
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# Import the model engine
from finance_model_v2 import (
    predict_ticker,
    predict_ticker_compare,
    daily_scan,
    daily_scan_both,
    get_all_symbols,
    get_strategy_mode,
    backtest_multi_strategy,
    SYMBOL_UNIVERSE,
    CACHE_DIR,
    ETF_TICKERS,
    HAS_LGBM,
    _ensure_cache_dir,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

PORT = 8000
HOST = "0.0.0.0"
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

# Daily movers cache (in-memory, refreshed on schedule + on-demand)
_movers_cache = {
    "data": [],
    "generated_at": None,
    "is_running": False,
}
_movers_lock = threading.Lock()


# =============================================================================
# SCHEDULED DAILY SCAN  (4:30 PM EST each weekday)
# =============================================================================

def _run_daily_scan(version=None):
    """Background task: run the full daily scan and cache results."""
    with _movers_lock:
        if _movers_cache["is_running"]:
            return
        _movers_cache["is_running"] = True

    ver_label = (version or 'auto').upper()
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] Starting daily scan ({ver_label})…")
    try:
        from finance_model_v2 import get_all_symbols as _get_syms
        symbols = _get_syms()
        results = []
        for i, sym in enumerate(symbols):
            try:
                r = predict_ticker(sym, cache_dir=CACHE_DIR, verbose=False, version=version)
                if "error" not in r:
                    results.append(r)
            except Exception as e:
                print(f"  [scan] {sym}: {e}")
            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(symbols)}] scanned…")

        if results:
            import pandas as pd
            df = pd.DataFrame(results).sort_values('pct_change', ascending=False).reset_index(drop=True)
            records = json.loads(df.to_json(orient='records'))

            # Save CSV and JSON files with model version in filename
            ver_tag = version or 'auto'
            timestamp = datetime.now().strftime('%Y%m%d_%H%M')
            csv_path = os.path.join(CACHE_DIR, f"daily_scan_{ver_tag}_{timestamp}.csv")
            json_path = os.path.join(CACHE_DIR, f"daily_scan_{ver_tag}_{timestamp}.json")
            _ensure_cache_dir(CACHE_DIR)
            df.to_csv(csv_path, index=False)
            df.to_json(json_path, orient='records', indent=2)
            print(f"  Saved: {csv_path}")

            with _movers_lock:
                _movers_cache["data"] = records
                _movers_cache["generated_at"] = datetime.now().isoformat()
                _movers_cache["model_version"] = ver_tag
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] Scan complete — {len(records)} symbols ({ver_label}).\n")
    except Exception as exc:
        print(f"[SCAN ERROR] {exc}")
    finally:
        with _movers_lock:
            _movers_cache["is_running"] = False


def _start_scheduler():
    """APScheduler: auto-run dual-model daily report after market close."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        # Full Lite+Pro report at 4:05 PM EST (Yahoo Finance updates ~4:01 PM)
        scheduler.add_job(
            _run_dual_report,
            'cron',
            day_of_week='mon-fri',
            hour=16, minute=5,
            timezone='US/Eastern',
            id='daily_dual_report',
            replace_existing=True,
        )
        scheduler.start()
        print("[Scheduler] Daily Lite+Pro report → 4:05 PM EST, Mon–Fri (auto after market close).")
    except ImportError:
        print("[Scheduler] apscheduler not installed — no auto-scheduling.")
        print("  Install: pip install apscheduler")
        print("  Manual trigger: GET /api/report?refresh=true")


def _load_latest_scan_from_disk():
    """On startup, load the most recent daily_scan_*.json from cache."""
    try:
        scan_files = sorted([
            f for f in os.listdir(CACHE_DIR)
            if f.startswith("daily_scan_") and f.endswith(".json")
        ])
        if scan_files:
            latest = os.path.join(CACHE_DIR, scan_files[-1])
            with open(latest) as f:
                data = json.load(f)
            with _movers_lock:
                _movers_cache["data"] = data
                _movers_cache["generated_at"] = scan_files[-1].replace(
                    "daily_scan_", "").replace(".json", "")
            print(f"[Startup] Loaded {len(data)} symbols from {scan_files[-1]}")
    except Exception as exc:
        print(f"[Startup] No cached scan: {exc}")


# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_cache_dir(CACHE_DIR)
    _start_scheduler()
    _load_latest_scan_from_disk()
    yield


app = FastAPI(
    title="Quantfolio",
    version="2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── API Routes ───

@app.get("/api/predict/{symbol}")
async def api_predict(symbol: str, version: str = None, strategy: str = None,
                      weight_rf: float = 0.8, weight_xgb: float = 0.2,
                      rolling_window: int = None):
    """
    Full ensemble prediction for a single ticker.
    Query params:
      ?version=v3              — Pro (stacking) or Lite (RF+XGB)
      ?strategy=auto           — auto (ETF→full, stock→buy_only), full, buy_only
      ?weight_rf=0.8&weight_xgb=0.2  — Lite model weights
      ?rolling_window=504             — training window (omit or 0 for all data)
    """
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 6:
        raise HTTPException(400, "Invalid ticker symbol")
    rw = rolling_window if rolling_window and rolling_window > 0 else None
    total_w = weight_rf + weight_xgb
    if total_w > 0:
        weight_rf /= total_w
        weight_xgb /= total_w
    # Validate version and strategy
    ver = version if version in ('v2', 'v3') else None
    strat = strategy if strategy in ('auto', 'full', 'buy_only') else 'auto'
    try:
        result = predict_ticker(symbol, cache_dir=CACHE_DIR, verbose=False,
                                version=ver, strategy=strat,
                                weight_rf=weight_rf, weight_xgb=weight_xgb,
                                rolling_window=rw)
    except Exception as exc:
        raise HTTPException(500, f"Prediction failed: {exc}")
    if "error" in result:
        raise HTTPException(404, result["error"])
    # Enrich with best backtest strategy if available
    best_map = _get_best_strategy_map()
    result['best_strategy'] = best_map.get(symbol)
    return JSONResponse(result)


@app.get("/api/predict-compare/{symbol}")
async def api_predict_compare(symbol: str, strategy: str = None):
    """
    Run BOTH Lite and Pro models on a single ticker.
    Returns side-by-side predictions with consensus signal.
    Query params:
      ?strategy=auto  — auto (ETF→full, stock→buy_only), full, buy_only
    """
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 6:
        raise HTTPException(400, "Invalid ticker symbol")
    strat = strategy if strategy in ('auto', 'full', 'buy_only') else 'auto'
    try:
        result = predict_ticker_compare(symbol, cache_dir=CACHE_DIR, verbose=False, strategy=strat)
    except Exception as exc:
        raise HTTPException(500, f"Comparison failed: {exc}")
    if "error" in result:
        raise HTTPException(404, result["error"])
    # Enrich with best backtest strategy if available
    best_map = _get_best_strategy_map()
    result['best_strategy'] = best_map.get(symbol)
    return JSONResponse(result)


# ─── Dual-model report cache ───
_report_cache = {
    "data": None,
    "generated_at": None,
    "is_running": False,
}
_report_lock = threading.Lock()


def _run_dual_report():
    """Background task: run dual-model scan and cache results."""
    with _report_lock:
        if _report_cache["is_running"]:
            return
        _report_cache["is_running"] = True

    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] Starting dual-model report…")
    try:
        report = daily_scan_both(cache_dir=CACHE_DIR)
        if report:
            with _report_lock:
                _report_cache["data"] = report
                _report_cache["generated_at"] = datetime.now().isoformat()
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] Dual report complete — {report['summary']['total_symbols']} symbols.\n")
    except Exception as exc:
        print(f"[DUAL REPORT ERROR] {exc}")
    finally:
        with _report_lock:
            _report_cache["is_running"] = False


@app.get("/api/report")
async def api_report(refresh: bool = False):
    """
    Dual-model daily report.
    GET /api/report               — return cached report
    GET /api/report?refresh=true  — trigger fresh dual-model scan
    """
    if refresh:
        with _report_lock:
            is_running = _report_cache["is_running"]
            snap_data = _report_cache["data"]
            snap_at = _report_cache["generated_at"]
        if not is_running:
            thread = threading.Thread(target=_run_dual_report, daemon=True)
            thread.start()
            return JSONResponse({
                "status": "scan_started",
                "message": "Dual-model report started. This may take 40-90 minutes.",
                "data": snap_data,
                "generated_at": snap_at,
            })
        else:
            return JSONResponse({
                "status": "scan_in_progress",
                "message": "Dual-model scan already running.",
                "data": snap_data,
                "generated_at": snap_at,
            })

    # Try loading from disk if cache is empty (call OUTSIDE lock — it acquires its own)
    if _report_cache["data"] is None:
        _load_latest_report_from_disk()

    with _report_lock:
        snap_data = _report_cache["data"]
        snap_at = _report_cache["generated_at"]

    best_strats = _get_best_strategy_map()
    return JSONResponse({
        "status": "ok",
        "data": snap_data,
        "generated_at": snap_at,
        "best_strategies": best_strats,
    })


def _load_latest_report_from_disk():
    """On startup/first request, load the most recent dual_report_*.json."""
    try:
        report_files = sorted([
            f for f in os.listdir(CACHE_DIR)
            if f.startswith("dual_report_") and f.endswith(".json")
        ])
        if report_files:
            latest = os.path.join(CACHE_DIR, report_files[-1])
            with open(latest) as f:
                data = json.load(f)
            with _report_lock:
                _report_cache["data"] = data
                _report_cache["generated_at"] = data.get('summary', {}).get('generated_at', report_files[-1])
            print(f"[Startup] Loaded dual report from {report_files[-1]}")
    except Exception as exc:
        print(f"[Startup] No cached dual report: {exc}")


@app.get("/api/movers")
async def api_movers(refresh: bool = False, version: str = None):
    """
    Daily movers list.
    GET /api/movers                        — return cached results
    GET /api/movers?refresh=true&version=v3 — trigger fresh scan with chosen model
    """
    ver = version if version in ('v2', 'v3') else None
    if refresh:
        with _movers_lock:
            is_running = _movers_cache["is_running"]
            snap_data = _movers_cache["data"]
            snap_at = _movers_cache["generated_at"]
        if not is_running:
            thread = threading.Thread(target=_run_daily_scan, args=(ver,), daemon=True)
            thread.start()
            ver_label = (ver or 'auto').upper()
            return JSONResponse({
                "status": "scan_started",
                "message": f"{ver_label} scan started. This may take a while.",
                "data": snap_data,
                "generated_at": snap_at,
            })
        else:
            return JSONResponse({
                "status": "scan_in_progress",
                "message": "Scan already running. Results update automatically.",
                "data": snap_data,
                "generated_at": snap_at,
            })
    with _movers_lock:
        snap_data = _movers_cache["data"]
        snap_at = _movers_cache["generated_at"]
        snap_count = len(snap_data)
        snap_ver = _movers_cache.get("model_version", "unknown")
    return JSONResponse({
        "status": "ok",
        "data": snap_data,
        "generated_at": snap_at,
        "count": snap_count,
        "model_version": snap_ver,
    })


@app.get("/api/symbols")
async def api_symbols():
    """Return the full symbol universe with ETF classification."""
    all_syms = get_all_symbols()
    return JSONResponse({
        "categories": SYMBOL_UNIVERSE,
        "all": all_syms,
        "count": len(all_syms),
        "etf_tickers": sorted(ETF_TICKERS),
        "strategy_info": {
            "default": "auto",
            "modes": {
                "auto": "ETF -> Full Signal (BUY+SELL), Stock -> Buy-Only (BUY only)",
                "full": "Full Signal: BUY and SELL on Z-score (best for ETFs)",
                "buy_only": "Buy-Only: BUY on Z-score, hold forever (best for stocks)",
            },
        },
    })


# ─── Backtest Chart ───

_bt_cache = {}       # symbol -> {'status': 'running'|'done'|'error', 'data': ..., 'error': ...}
_bt_progress = {}    # symbol -> {'v2': '5/44', 'v3': '12/44'}  — live progress
_bt_lock = threading.Lock()


def _run_backtest_chart(symbol):
    """Background: run multi-strategy backtests for Lite and Pro IN PARALLEL."""
    with _bt_lock:
        _bt_progress[symbol] = {}

    def v2_cb(step, total):
        with _bt_lock:
            _bt_progress.setdefault(symbol, {})['v2'] = f'{step}/{total}'

    def v3_cb(step, total):
        with _bt_lock:
            _bt_progress.setdefault(symbol, {})['v3'] = f'{step}/{total}'

    try:
        print(f"\n[Backtest] Starting {symbol} — Lite + Pro in parallel…")

        with ThreadPoolExecutor(max_workers=2) as pool:
            v2_fut = pool.submit(backtest_multi_strategy, symbol,
                                 cache_dir=CACHE_DIR, version='v2', progress_cb=v2_cb)
            v3_fut = None
            if HAS_LGBM:
                v3_fut = pool.submit(backtest_multi_strategy, symbol,
                                     cache_dir=CACHE_DIR, version='v3', progress_cb=v3_cb)

            # Collect results (re-raises exceptions from threads)
            try:
                v2_result = v2_fut.result()
            except Exception as e:
                print(f"[Backtest] V2 failed for {symbol}: {e}")
                v2_result = None

            v3_result = None
            if v3_fut:
                try:
                    v3_result = v3_fut.result()
                except Exception as e:
                    print(f"[Backtest] V3 failed for {symbol}: {e}")
                    v3_result = None
                # Discard if V3 fell back to V2 (no LightGBM)
                if v3_result and v3_result.get('version') == 'v2':
                    v3_result = None

        if not v2_result and not v3_result:
            with _bt_lock:
                _bt_cache[symbol] = {'status': 'error', 'error': 'No data available for backtest'}
            return

        # Use available dates (both should match, trim to shorter if not)
        primary = v2_result or v3_result
        dates = primary['dates']
        if v2_result and v3_result:
            min_len = min(len(v2_result['dates']), len(v3_result['dates']))
            dates = v2_result['dates'][:min_len]
            for key in ('buyhold',):
                v2_result[key] = v2_result[key][:min_len]
                v3_result[key] = v3_result[key][:min_len]
            for key in ('full', 'buy_only'):
                v2_result[key]['portfolio'] = v2_result[key]['portfolio'][:min_len]
                v3_result[key]['portfolio'] = v3_result[key]['portfolio'][:min_len]

        start_date = primary.get('start_date') or (dates[0] if dates else None)

        result = {
            'symbol': symbol,
            'start_date': start_date,
            'dates': dates,
            'period_days': len(dates),
            'strategies': {
                'buyhold': {
                    'portfolio': primary['buyhold'][:len(dates)],
                    **primary['buyhold_stats'],
                },
                'lite_buyonly': v2_result['buy_only'] if v2_result else None,
                'lite_full': v2_result['full'] if v2_result else None,
                'pro_buyonly': v3_result['buy_only'] if v3_result else None,
                'pro_full': v3_result['full'] if v3_result else None,
            },
        }

        # Save to disk cache
        _ensure_cache_dir(CACHE_DIR)
        cache_path = os.path.join(CACHE_DIR, f"backtest_chart_{symbol}.json")
        with open(cache_path, 'w') as f:
            json.dump(result, f)

        with _bt_lock:
            _bt_cache[symbol] = {'status': 'done', 'data': result}
        print(f"[Backtest] {symbol} complete.\n")

    except Exception as e:
        print(f"[Backtest ERROR] {symbol}: {e}")
        import traceback; traceback.print_exc()
        with _bt_lock:
            _bt_cache[symbol] = {'status': 'error', 'error': str(e)}
    finally:
        with _bt_lock:
            _bt_progress.pop(symbol, None)


@app.get("/api/backtest-chart/{symbol}")
async def api_backtest_chart(symbol: str, refresh: bool = False):
    """
    Walk-forward backtest comparison chart data.
    Returns equity curves for 5 strategies: B&H, Lite Buy-Only, Lite Full, Pro Buy-Only, Pro Full.
    First request triggers background computation; poll until status='done'.
    Results cached to disk for 7 days.
    """
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 6:
        raise HTTPException(400, "Invalid ticker symbol")

    # Check in-memory cache
    with _bt_lock:
        cached = _bt_cache.get(symbol)

    if cached and not refresh:
        if cached['status'] == 'done':
            return JSONResponse({'status': 'done', **cached['data']})
        elif cached['status'] == 'running':
            with _bt_lock:
                prog = dict(_bt_progress.get(symbol, {}))  # snapshot under lock
            parts = []
            if 'v2' in prog:
                parts.append(f'Lite: retrain {prog["v2"]}')
            if 'v3' in prog:
                parts.append(f'Pro: retrain {prog["v3"]}')
            return JSONResponse({
                'status': 'running', 'symbol': symbol,
                'progress': ' | '.join(parts) if parts else 'Starting\u2026',
            })
        elif cached['status'] == 'error':
            # Clear error so user can retry
            with _bt_lock:
                _bt_cache.pop(symbol, None)  # safe pop instead of del
            return JSONResponse({'status': 'error', 'error': cached.get('error', 'Unknown error')})

    # Check disk cache (valid for 7 days)
    if not refresh:
        cache_path = os.path.join(CACHE_DIR, f"backtest_chart_{symbol}.json")
        if os.path.exists(cache_path):
            try:
                age_days = (time.time() - os.path.getmtime(cache_path)) / 86400
                if age_days < 7:
                    with open(cache_path) as f:
                        data = json.load(f)
                    with _bt_lock:
                        _bt_cache[symbol] = {'status': 'done', 'data': data}
                    return JSONResponse({'status': 'done', **data})
            except Exception:
                pass

    # Start background computation (thread spawned inside lock to prevent duplicates)
    with _bt_lock:
        already_running = _bt_cache.get(symbol, {}).get('status') == 'running'
        if not already_running:
            _bt_cache[symbol] = {'status': 'running'}
            thread = threading.Thread(target=_run_backtest_chart, args=(symbol,), daemon=True)
            thread.start()

    return JSONResponse({'status': 'running', 'symbol': symbol})


# ─── Backtest Library (batch runner + summary) ───

_batch_state = {
    "is_running": False,
    "total": 0,
    "completed": 0,
    "current_symbol": None,
    "completed_symbols": [],
    "failed_symbols": [],
    "skipped_cached": 0,
    "started_at": None,
    "error": None,
}
_batch_lock = threading.Lock()

BACKTEST_CACHE_TTL_DAYS = 7


def _run_backtest_batch():
    """Background: run backtests for all uncached symbols sequentially."""
    try:
        all_syms = get_all_symbols()
        to_run = []
        already_cached = []
        for sym in all_syms:
            path = os.path.join(CACHE_DIR, f"backtest_chart_{sym}.json")
            if os.path.exists(path):
                try:
                    age = (time.time() - os.path.getmtime(path)) / 86400
                    if age < BACKTEST_CACHE_TTL_DAYS:
                        already_cached.append(sym)
                        continue
                except Exception:
                    pass
            to_run.append(sym)

        with _batch_lock:
            _batch_state["total"] = len(to_run)
            _batch_state["completed"] = 0
            _batch_state["current_symbol"] = None
            _batch_state["skipped_cached"] = len(already_cached)

        print(f"\n[Batch] Starting backtest batch — {len(to_run)} symbols to run, "
              f"{len(already_cached)} already cached.")

        for i, sym in enumerate(to_run):
            with _batch_lock:
                _batch_state["current_symbol"] = sym
            try:
                print(f"[Batch] [{i+1}/{len(to_run)}] {sym}…")
                _run_backtest_chart(sym)
                # _run_backtest_chart catches its own exceptions, so check result
                with _bt_lock:
                    result_status = _bt_cache.get(sym, {}).get('status')
                with _batch_lock:
                    _batch_state["completed"] = i + 1
                    if result_status == 'done':
                        _batch_state["completed_symbols"].append(sym)
                    else:
                        err = 'internal error'
                        with _bt_lock:
                            err = _bt_cache.get(sym, {}).get('error', err)
                        print(f"[Batch] {sym} failed internally: {err}")
                        _batch_state["failed_symbols"].append(sym)
            except Exception as e:
                print(f"[Batch] {sym} failed: {e}")
                with _batch_lock:
                    _batch_state["completed"] = i + 1
                    _batch_state["failed_symbols"].append(sym)

        with _batch_lock:
            succeeded = len(_batch_state['completed_symbols'])
            failed = len(_batch_state['failed_symbols'])
        print(f"[Batch] Complete — {succeeded} succeeded, {failed} failed.\n")

    except Exception as e:
        print(f"[Batch] FATAL ERROR: {e}")
        import traceback; traceback.print_exc()
        with _batch_lock:
            _batch_state["error"] = str(e)
    finally:
        with _batch_lock:
            _batch_state["is_running"] = False
            _batch_state["current_symbol"] = None


def _load_library_summary():
    """Scan disk cache and build lightweight summary (no portfolio arrays)."""
    all_syms = get_all_symbols()
    items = []
    strat_keys = ['buyhold', 'lite_buyonly', 'lite_full', 'pro_buyonly', 'pro_full']

    for sym in all_syms:
        path = os.path.join(CACHE_DIR, f"backtest_chart_{sym}.json")
        if not os.path.exists(path):
            continue
        try:
            age_days = (time.time() - os.path.getmtime(path)) / 86400
            if age_days >= BACKTEST_CACHE_TTL_DAYS:
                continue
            with open(path) as f:
                data = json.load(f)
            # Build summary — strip large arrays
            strats = {}
            best_sharpe = -999
            best_strat = None
            for sk in strat_keys:
                s = data.get('strategies', {}).get(sk)
                if s:
                    info = {
                        'return_pct': s.get('return_pct'),
                        'sharpe': s.get('sharpe'),
                        'max_drawdown': s.get('max_drawdown'),
                        'buys': s.get('buys', 0),
                        'sells': s.get('sells', 0),
                    }
                    strats[sk] = info
                    if info['sharpe'] is not None and info['sharpe'] > best_sharpe:
                        best_sharpe = info['sharpe']
                        best_strat = sk
            items.append({
                'symbol': data.get('symbol', sym),
                'start_date': data.get('start_date'),
                'period_days': data.get('period_days'),
                'strategies': strats,
                'best_sharpe_strategy': best_strat,
                'best_sharpe_value': round(best_sharpe, 2) if best_sharpe > -999 else None,
                'cache_age_hours': round(age_days * 24, 1),
            })
        except Exception as e:
            print(f"[Library] Error loading {sym}: {e}")
    return items


_STRATEGY_LABELS = {
    'buyhold': 'Buy & Hold',
    'lite_buyonly': 'Lite Buy-Only',
    'lite_full': 'Lite Full Signal',
    'pro_buyonly': 'Pro Buy-Only',
    'pro_full': 'Pro Full Signal',
}

def _get_best_strategy_map():
    """Quick lookup: symbol → best strategy info from cached backtest results."""
    result = {}
    items = _load_library_summary()
    for item in items:
        sk = item.get('best_sharpe_strategy')
        if sk:
            result[item['symbol']] = {
                'key': sk,
                'name': _STRATEGY_LABELS.get(sk, sk),
                'sharpe': item.get('best_sharpe_value'),
            }
    return result


@app.get("/api/backtest-library")
async def api_backtest_library():
    """Summary stats for all cached backtests (no portfolio arrays)."""
    all_syms = get_all_symbols()
    items = _load_library_summary()
    with _batch_lock:
        batch_running = _batch_state["is_running"]
    return JSONResponse({
        "status": "ok",
        "symbols_total": len(all_syms),
        "symbols_cached": len(items),
        "batch_running": batch_running,
        "data": items,
    })


@app.post("/api/backtest-batch")
async def api_backtest_batch():
    """Trigger batch backtest for all uncached symbols."""
    with _batch_lock:
        if _batch_state["is_running"]:
            return JSONResponse({
                "status": "batch_in_progress",
                "completed": _batch_state["completed"],
                "total": _batch_state["total"],
                "current_symbol": _batch_state["current_symbol"],
            })
        # Count cached BEFORE starting thread so we can pre-set total
        all_syms = get_all_symbols()
        cached = 0
        for sym in all_syms:
            path = os.path.join(CACHE_DIR, f"backtest_chart_{sym}.json")
            if os.path.exists(path):
                try:
                    age = (time.time() - os.path.getmtime(path)) / 86400
                    if age < BACKTEST_CACHE_TTL_DAYS:
                        cached += 1
                except Exception:
                    pass
        to_run = len(all_syms) - cached

        # Initialize ALL state before starting thread to avoid race conditions
        _batch_state["is_running"] = True
        _batch_state["total"] = to_run
        _batch_state["completed"] = 0
        _batch_state["current_symbol"] = None
        _batch_state["completed_symbols"] = []
        _batch_state["failed_symbols"] = []
        _batch_state["skipped_cached"] = cached
        _batch_state["started_at"] = time.time()
        _batch_state["error"] = None

    thread = threading.Thread(target=_run_backtest_batch, daemon=True)
    thread.start()

    return JSONResponse({
        "status": "batch_started",
        "symbols_total": len(all_syms),
        "already_cached": cached,
        "to_run": to_run,
    })


@app.get("/api/backtest-batch/status")
async def api_backtest_batch_status():
    """Poll batch backtest progress."""
    with _batch_lock:
        snap = dict(_batch_state)
        snap["completed_symbols"] = list(snap["completed_symbols"])
        snap["failed_symbols"] = list(snap["failed_symbols"])
    # Add per-symbol progress if running
    current_prog = None
    if snap["current_symbol"]:
        with _bt_lock:
            prog = dict(_bt_progress.get(snap["current_symbol"], {}))
        parts = []
        if 'v2' in prog:
            parts.append(f'Lite: {prog["v2"]}')
        if 'v3' in prog:
            parts.append(f'Pro: {prog["v3"]}')
        current_prog = ' | '.join(parts) if parts else None
    elapsed = round(time.time() - snap["started_at"]) if snap["started_at"] else 0
    return JSONResponse({
        "status": "running" if snap["is_running"] else "done",
        "total": snap["total"],
        "completed": snap["completed"],
        "skipped_cached": snap.get("skipped_cached", 0),
        "current_symbol": snap["current_symbol"],
        "current_progress": current_prog,
        "completed_symbols": snap["completed_symbols"],
        "failed_symbols": snap["failed_symbols"],
        "elapsed_seconds": elapsed,
        "error": snap.get("error"),
    })


# ─── Serve Frontend ───

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse(
        '<html><body style="background:#020617;color:#94a3b8;font-family:monospace;'
        'padding:40px;text-align:center">'
        '<h2>Finance Predictor API is running</h2>'
        '<p>Place index.html in the <code>frontend/</code> folder, or use the API directly:</p>'
        '<p><a href="/api/predict/AAPL" style="color:#60a5fa">/api/predict/AAPL</a></p>'
        '<p><a href="/api/movers" style="color:#60a5fa">/api/movers</a></p>'
        '<p><a href="/api/symbols" style="color:#60a5fa">/api/symbols</a></p>'
        '</body></html>'
    )

# Mount static files if directory exists
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    index_check = os.path.join(FRONTEND_DIR, "index.html")
    print(f"\n  Starting server at http://localhost:{PORT}")
    print(f"  Frontend dir: {FRONTEND_DIR}")
    print(f"  index.html exists: {os.path.exists(index_check)}")
    print(f"  Cache directory: {CACHE_DIR}\n")
    uvicorn.run(app, host=HOST, port=PORT)
