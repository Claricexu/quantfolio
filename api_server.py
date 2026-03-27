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
import threading
from datetime import datetime
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
    SYMBOL_UNIVERSE,
    CACHE_DIR,
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
            except Exception:
                pass
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
    """APScheduler job: 4:30 PM US/Eastern, Mon–Fri."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            _run_daily_scan,
            'cron',
            day_of_week='mon-fri',
            hour=16, minute=30,
            timezone='US/Eastern',
            id='daily_scan',
            replace_existing=True,
        )
        scheduler.start()
        print("[Scheduler] Daily scan → 4:30 PM EST, Mon–Fri.")
    except ImportError:
        print("[Scheduler] apscheduler not installed — no auto-scheduling.")
        print("  Install: pip install apscheduler")
        print("  Manual trigger: GET /api/movers?refresh=true")


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
async def api_predict(symbol: str, version: str = None, weight_rf: float = 0.8,
                      weight_xgb: float = 0.2, rolling_window: int = None):
    """
    Full ensemble prediction for a single ticker.
    Query params:
      ?version=v3              — Pro (stacking) or Lite (RF+XGB)
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
    # Validate version
    ver = version if version in ('v2', 'v3') else None
    try:
        result = predict_ticker(symbol, cache_dir=CACHE_DIR, verbose=False,
                                version=ver, weight_rf=weight_rf, weight_xgb=weight_xgb,
                                rolling_window=rw)
    except Exception as exc:
        raise HTTPException(500, f"Prediction failed: {exc}")
    if "error" in result:
        raise HTTPException(404, result["error"])
    return JSONResponse(result)


@app.get("/api/predict-compare/{symbol}")
async def api_predict_compare(symbol: str):
    """
    Run BOTH Lite and Pro models on a single ticker.
    Returns side-by-side predictions with consensus signal.
    """
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 6:
        raise HTTPException(400, "Invalid ticker symbol")
    try:
        result = predict_ticker_compare(symbol, cache_dir=CACHE_DIR, verbose=False)
    except Exception as exc:
        raise HTTPException(500, f"Comparison failed: {exc}")
    if "error" in result:
        raise HTTPException(404, result["error"])
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
        if not is_running:
            thread = threading.Thread(target=_run_dual_report, daemon=True)
            thread.start()
            return JSONResponse({
                "status": "scan_started",
                "message": "Dual-model report started. This may take 40-90 minutes.",
                "data": _report_cache["data"],
                "generated_at": _report_cache["generated_at"],
            })
        else:
            return JSONResponse({
                "status": "scan_in_progress",
                "message": "Dual-model scan already running.",
                "data": _report_cache["data"],
                "generated_at": _report_cache["generated_at"],
            })

    # Try loading from disk if cache is empty
    if _report_cache["data"] is None:
        _load_latest_report_from_disk()

    return JSONResponse({
        "status": "ok",
        "data": _report_cache["data"],
        "generated_at": _report_cache["generated_at"],
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
        if not is_running:
            thread = threading.Thread(target=_run_daily_scan, args=(ver,), daemon=True)
            thread.start()
            ver_label = (ver or 'auto').upper()
            return JSONResponse({
                "status": "scan_started",
                "message": f"{ver_label} scan started. This may take a while.",
                "data": _movers_cache["data"],
                "generated_at": _movers_cache["generated_at"],
            })
        else:
            return JSONResponse({
                "status": "scan_in_progress",
                "message": "Scan already running. Results update automatically.",
                "data": _movers_cache["data"],
                "generated_at": _movers_cache["generated_at"],
            })
    return JSONResponse({
        "status": "ok",
        "data": _movers_cache["data"],
        "generated_at": _movers_cache["generated_at"],
        "count": len(_movers_cache["data"]),
        "model_version": _movers_cache.get("model_version", "unknown"),
    })


@app.get("/api/symbols")
async def api_symbols():
    """Return the full symbol universe."""
    return JSONResponse({
        "categories": SYMBOL_UNIVERSE,
        "all": get_all_symbols(),
        "count": len(get_all_symbols()),
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
