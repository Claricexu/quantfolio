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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

# Load .env (if present) before any os.environ reads below.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional — env vars still work without it

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

# Round 7c verification round 8: canonical (sector, industry_group, industry)
# classifier. Imported at module load — leaf module, stdlib-only, no SEC or
# screener dependency, so it's safe to use even if HAS_SCREENER is False.
from classifier import classify as _classify_symbol

# ─── Optional: fundamental screener (Good Firm Framework) ────────────────────
# Loads edgar_fetcher + fundamental_screener. Failure here does NOT affect the
# three existing tabs — endpoints below simply return 503 if unavailable.
try:
    # fundamental_screener import kept as a module-availability gate even
    # though Bucket 2 serves /api/screener from the CSV via verdict_provider.
    # run_full_screen stays importable for the leaders rebuild subprocess.
    from fundamental_screener import run_full_screen as _screener_run_full  # noqa: F401
    from edgar_fetcher import (
        fetch_all as _edgar_fetch_all,
        get_db as _edgar_get_db,
        load_tickers_from_csv as _edgar_load_tickers,
    )
    # Bucket 2 (2026-04-21): CSV-backed unified verdict reader. All three
    # tabs (Lookup, Daily Report, Leader Detector) flow through this module
    # so they can never disagree on what verdict/reason a symbol has.
    import verdict_provider as _verdict_provider
    HAS_SCREENER = True
except Exception as _screener_err:
    print(f"[Screener] Not available: {_screener_err}")
    HAS_SCREENER = False

# =============================================================================
# CONFIGURATION
# =============================================================================

PORT = 8000
HOST = "0.0.0.0"
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

# =============================================================================
# EMAIL ALERTS — configured via .env (see .env.example)
# =============================================================================
# To use Gmail: go to https://myaccount.google.com/apppasswords and generate
# an App Password (requires 2-Step Verification enabled). Put it in SMTP_PASSWORD
# in your local .env file — do NOT hardcode here.
SMTP_ENABLED  = os.environ.get("SMTP_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
SMTP_SERVER   = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ALERT_TO      = [e.strip() for e in os.environ.get("ALERT_TO", "").split(",") if e.strip()]
ALERT_SUBJECT = os.environ.get("ALERT_SUBJECT", "Quantfolio Signal Brief")


_BUY_VALIDATING_KEYS_PRO  = ('pro_buyonly', 'pro_full')
_BUY_VALIDATING_KEYS_LITE = ('lite_buyonly', 'lite_full')
_SELL_GATE_KEYS           = ('buyhold', 'lite_buyonly', 'pro_buyonly')


def _row_signals(r):
    """Extract (lite_sig, pro_sig) from a daily-report row, defaulting to HOLD."""
    v2 = r.get('v2') or {}
    v3 = r.get('v3') or {}
    lite_sig = (v2.get('signal') or 'HOLD').upper()
    pro_sig  = (v3.get('signal') or 'HOLD').upper()
    return lite_sig, pro_sig


def _classify_alert(lite_sig, pro_sig, best_key):
    """Decide whether a row qualifies for a BUY or SELL email alert.

    Returns ('BUY'|'SELL', path_label) when the row qualifies, or
    (None, suppression_reason) otherwise. The path label / reason string is
    suitable for direct logging — see the [Alert] log lines emitted in
    ``_send_signal_alerts``. Pure function: no globals, no I/O — so this is
    table-tested in tests/unit/test_signal_alerts.py.

    Rules (Round 8b — backtest-validated single-model paths):
      BUY fires when ANY of:
        (a) Both Lite and Pro signaled BUY (consensus).
        (b) Only Pro signaled BUY (Lite=HOLD) AND best ∈ {pro_buyonly, pro_full}.
        (c) Only Lite signaled BUY (Pro=HOLD) AND best ∈ {lite_buyonly, lite_full}.
      SELL fires when:
        Hard gate — best ∈ {buyhold, lite_buyonly, pro_buyonly} → suppress.
        Path A — best == pro_full AND Pro=SELL (Lite=HOLD or SELL, not BUY).
        Path B — best == lite_full AND Lite=SELL (Pro=HOLD or SELL, not BUY).
      Conflict (Lite/Pro disagree BUY vs SELL) → never fires.
      best_strategy null/missing → only path (a) can fire; SELL never fires.
    """
    if lite_sig == 'HOLD' and pro_sig == 'HOLD':
        return None, 'no model signals'
    if {lite_sig, pro_sig} == {'BUY', 'SELL'}:
        return None, f'model conflict (Lite={lite_sig} Pro={pro_sig})'

    # BUY paths
    if lite_sig == 'BUY' and pro_sig == 'BUY':
        return 'BUY', 'consensus (Lite+Pro)'
    if pro_sig == 'BUY' and lite_sig == 'HOLD':
        if best_key is None:
            return None, 'best_strategy null/missing'
        if best_key in _BUY_VALIDATING_KEYS_PRO:
            return 'BUY', f'pro-only validated_by={best_key}'
        return None, f"best_strategy={best_key} doesn't validate signal direction"
    if lite_sig == 'BUY' and pro_sig == 'HOLD':
        if best_key is None:
            return None, 'best_strategy null/missing'
        if best_key in _BUY_VALIDATING_KEYS_LITE:
            return 'BUY', f'lite-only validated_by={best_key}'
        return None, f"best_strategy={best_key} doesn't validate signal direction"

    # SELL paths
    if pro_sig == 'SELL' or lite_sig == 'SELL':
        if best_key in _SELL_GATE_KEYS:
            return None, f'SELL gate active (best_strategy={best_key})'
        if best_key == 'pro_full' and pro_sig == 'SELL':
            return 'SELL', 'pro-full-signal'
        if best_key == 'lite_full' and lite_sig == 'SELL':
            return 'SELL', 'lite-full-signal'
        if best_key is None:
            return None, 'best_strategy null/missing'
        return None, f"best_strategy={best_key} doesn't validate signal direction"

    return None, 'no model signals'


def _send_signal_alerts(report):
    """After a dual report completes, email any backtest-validated BUY or SELL signals."""
    if not SMTP_ENABLED:
        return
    if not report or 'data' not in report:
        return

    rows = report['data'] or []
    best_map = _get_best_strategy_map()

    buys, sells = [], []
    for r in rows:
        sym = r.get('symbol', '?')
        lite_sig, pro_sig = _row_signals(r)
        bs = best_map.get(sym)
        best_key = bs.get('key') if bs else None
        verdict, reason = _classify_alert(lite_sig, pro_sig, best_key)
        if verdict == 'BUY':
            print(f"[Alert] {sym} BUY: path={reason}")
            buys.append(r)
        elif verdict == 'SELL':
            print(f"[Alert] {sym} SELL: path={reason}")
            sells.append(r)
        else:
            print(f"[Alert] {sym} suppressed: {reason}")

    if not buys and not sells:
        print("[Alert] No backtest-validated signals today — no email sent.")
        return

    # Build email body
    date_str = datetime.now().strftime('%B %d, %Y')

    def _best_str(sym):
        b = best_map.get(sym)
        return b['name'] if b else ''

    def _svr_str(a):
        svr = a.get('svr')
        return f"{svr:.1f}x" if svr is not None else ''

    # Plain text version
    lines = [f"Quantfolio Signal Brief — {date_str}", "=" * 50, ""]
    if buys:
        lines.append(f"BUY SIGNALS ({len(buys)}):")
        lines.append("-" * 60)
        for a in buys:
            v2c = a['v2']['pct_change'] if a.get('v2') else 0
            v3c = a['v3']['pct_change'] if a.get('v3') else 0
            bs = _best_str(a['symbol'])
            sv = _svr_str(a)
            lines.append(f"  {a['symbol']:<6}  Price: ${a['current_price']:<10}  "
                         f"Lite: {v2c:+.2f}%  Pro: {v3c:+.2f}%"
                         f"{'  Best: ' + bs if bs else ''}"
                         f"{'  SVR: ' + sv if sv else ''}")
        lines.append("")
    if sells:
        lines.append(f"SELL SIGNALS ({len(sells)}):")
        lines.append("-" * 60)
        for a in sells:
            v2c = a['v2']['pct_change'] if a.get('v2') else 0
            v3c = a['v3']['pct_change'] if a.get('v3') else 0
            bs = _best_str(a['symbol'])
            sv = _svr_str(a)
            lines.append(f"  {a['symbol']:<6}  Price: ${a['current_price']:<10}  "
                         f"Lite: {v2c:+.2f}%  Pro: {v3c:+.2f}%"
                         f"{'  Best: ' + bs if bs else ''}"
                         f"{'  SVR: ' + sv if sv else ''}")
        lines.append("")
    lines.append(f"Total scanned: {report['summary']['total_symbols']} symbols")
    lines.append(f"Market sentiment: {report['summary'].get('market_sentiment', 'N/A')}")
    lines.append("")
    lines.append("— Quantfolio (auto-generated, do not reply)")
    text_body = "\n".join(lines)

    # HTML version (nicer in most email clients)
    def _row(a, color):
        v2c = a['v2']['pct_change'] if a.get('v2') else 0
        v3c = a['v3']['pct_change'] if a.get('v3') else 0
        bs = _best_str(a['symbol'])
        sv = _svr_str(a)
        return (f'<tr><td style="padding:6px 12px;font-weight:700">{a["symbol"]}</td>'
                f'<td style="padding:6px 12px">${a["current_price"]}</td>'
                f'<td style="padding:6px 12px;color:{color};font-weight:700">'
                f'{a["consensus_signal"]}</td>'
                f'<td style="padding:6px 12px">{v2c:+.2f}%</td>'
                f'<td style="padding:6px 12px">{v3c:+.2f}%</td>'
                f'<td style="padding:6px 12px;font-size:12px">{bs or "—"}</td>'
                f'<td style="padding:6px 12px;font-size:12px">{sv or "—"}</td></tr>')

    rows_html = ""
    for a in buys:
        rows_html += _row(a, "#22c55e")
    for a in sells:
        rows_html += _row(a, "#ef4444")

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;color:#1e293b">
      <h2 style="color:#0f172a;border-bottom:2px solid #2d8b8b;padding-bottom:8px">
        Quantfolio Signal Brief — {date_str}
      </h2>
      <p style="color:#475569;font-size:14px">
        <strong>{len(buys)}</strong> BUY and <strong>{len(sells)}</strong> SELL
        high-conviction signals.
      </p>
      <table style="border-collapse:collapse;width:100%;font-size:14px;margin:16px 0">
        <tr style="background:#f1f5f9;font-weight:600;font-size:12px;text-transform:uppercase;color:#64748b">
          <th style="padding:8px 12px;text-align:left">Symbol</th>
          <th style="padding:8px 12px;text-align:left">Price</th>
          <th style="padding:8px 12px;text-align:left">Signal</th>
          <th style="padding:8px 12px;text-align:left">Lite</th>
          <th style="padding:8px 12px;text-align:left">Pro</th>
          <th style="padding:8px 12px;text-align:left">Best Strategy</th>
          <th style="padding:8px 12px;text-align:left">SVR</th>
        </tr>
        {rows_html}
      </table>
      <p style="color:#94a3b8;font-size:12px;margin-top:24px">
        Scanned {report['summary']['total_symbols']} symbols &bull;
        Sentiment: {report['summary'].get('market_sentiment', 'N/A')} &bull;
        Auto-generated by Quantfolio
      </p>
    </div>"""

    # Send
    subject = f"{ALERT_SUBJECT} — {len(buys)} BUY, {len(sells)} SELL ({date_str})"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = ", ".join(ALERT_TO)
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
        print(f"[Alert] Email sent to {', '.join(ALERT_TO)} — {len(buys)} BUY, {len(sells)} SELL.")
    except Exception as e:
        print(f"[Alert] Email failed: {e}")


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
    """APScheduler: auto-run dual-model daily report after market close,
    plus quarterly Leader Detector rebuild after 10-Q filing season."""
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
        # Quarterly Leader Detector rebuild (Phase 1.0 → 1.4): Feb/May/Aug/Nov
        # 15 at 2 AM EST. These dates fall ~2 weeks after the typical 10-Q
        # filing deadline (40 days after quarter-end), so SEC XBRL data for
        # the most-recent quarter is available. Cold run ~3.5h; warm reruns
        # (checkpointed + 90-day XBRL TTL) ~10 min. max_instances=1 prevents
        # overlap if a prior run is still going.
        scheduler.add_job(
            _leaders_rebuild_worker,
            'cron',
            month='2,5,8,11', day=15,
            hour=2, minute=0,
            timezone='US/Eastern',
            id='quarterly_leader_rebuild',
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        print("[Scheduler] Daily Lite+Pro report → 4:05 PM EST, Mon–Fri (auto after market close).")
        print("[Scheduler] Quarterly Leader Detector rebuild → Feb/May/Aug/Nov 15 at 2 AM EST.")
    except ImportError:
        print("[Scheduler] apscheduler not installed — no auto-scheduling.")
        print("  Install: pip install apscheduler")
        print("  Manual trigger: GET /api/report?refresh=true  (daily report)")
        print("  Manual trigger: POST /api/leaders/rebuild     (leader rebuild)")


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
    # Round 8b: surface backtest-library coverage so the alert engine's
    # validation step is observable. Single-model BUY/SELL paths require a
    # populated best_strategy entry — coverage gaps explain "expected fire,
    # nothing happened" cases without needing to grep the per-ticker logs.
    try:
        n_strats = len(_get_best_strategy_map())
        n_total = len(get_all_symbols())
        pct = (n_strats * 100 // n_total) if n_total else 0
        print(f"[Scheduler] best_strategy_map populated: {n_strats} of {n_total} tickers ({pct}%)")
    except Exception as exc:
        print(f"[Scheduler] best_strategy_map probe failed: {exc}")
    # Bucket 2: warn once if screener_results.csv predates the tests_json /
    # dealbreakers_json columns. Non-fatal — verdict_provider handles the
    # missing columns by rendering dashes in the test-dot row / flag chips.
    if HAS_SCREENER:
        try:
            ok, missing = _verdict_provider.csv_has_required_columns()
            if not ok:
                print(
                    "[startup] screener_results.csv missing "
                    + "/".join(missing)
                    + " columns — test-dot row and flag chips will render "
                    "as dashes until next screener run. Regenerate with: "
                    "python fundamental_screener.py --universe "
                    "universe_prescreened.csv --csv-out screener_results.csv"
                )
        except Exception as exc:  # never block startup on this
            print(f"[startup] verdict_provider column check failed: {exc}")
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


def _inject_classifier_fields(result: dict, symbol: str) -> dict:
    """Round 7c verification round 8 — overlay classifier-derived
    (sector, industry_group, industry) onto a /api/predict[-compare] result
    so Ticker Lookup shows the same canonical labels as Leader Detector.

    Resolution order:
      1. Look up SIC from the screener CSV via verdict_provider (cheap —
         mtime-keyed in-process cache; only re-reads CSV on file change).
      2. Call classifier.classify(symbol, sic, yahoo_industry).
      3. If classifier returns a non-Unknown sector (i.e. ticker is in
         TICKER_OVERRIDES OR the SIC matched a range), OVERWRITE result's
         sector + industry, and set industry_group. Override tickers are
         keyed on the symbol so they win regardless of SIC.
      4. If classifier returns Unknown (ETFs, off-list stocks with no SEC
         filings), KEEP Yahoo's sector/industry — wright's review:
         "'Unknown' on the verdict card for an ETF is a worse UX
         regression than showing Yahoo's slightly-different taxonomy."
         Leave industry_group absent so the frontend renders an em-dash.

    No changes to finance_model_v2.py per the round constraint — the model
    result is mutated only here in the API layer.
    """
    sic = None
    if HAS_SCREENER:
        try:
            row = _verdict_provider.load_screener_index().get(symbol.upper())
            if row:
                sic = row.get("sic")
                # Round 7c-2: piggyback the screener-row lookup to surface
                # pe_trailing on Ticker Lookup's P/E card. verdict_provider's
                # _FLOAT_COLS whitelist doesn't include pe_trailing, so the
                # row value may be a raw CSV string — coerce to float here.
                pe_raw = row.get("pe_trailing")
                if pe_raw not in (None, ""):
                    try:
                        result["pe_trailing"] = float(pe_raw)
                    except (TypeError, ValueError):
                        result["pe_trailing"] = None
                # Round 8a Phase 3: surface peer_median_svr on compare results
                # so the live SVR card on Ticker Lookup can annotate "peer 13.3x"
                # alongside the live yfinance value. The verdict-card SVR row
                # was removed in the same change to eliminate the dual-source
                # divergence (live yfinance vs CSV-frozen). verdict_provider
                # whitelists peer_median_svr, so the row value is already
                # float-typed when present; coerce defensively for the cached
                # path where the row dict may still hold a CSV string.
                psvr_raw = row.get("peer_median_svr")
                if psvr_raw not in (None, ""):
                    try:
                        result["peer_median_svr"] = float(psvr_raw)
                    except (TypeError, ValueError):
                        result["peer_median_svr"] = None
        except Exception:
            pass
    sec, ig, ind = _classify_symbol(symbol, sic, result.get("industry"))
    if sec != "Unknown":
        result["sector"] = sec
        result["industry_group"] = ig
        result["industry"] = ind
    return result


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
    _inject_classifier_fields(result, symbol)
    return JSONResponse(result)


def _is_cached_report_acceptable(report_timestamp: datetime, now: datetime | None = None) -> bool:
    """Return True if the cached report is the freshest available.

    Daily Report scheduler runs Mon-Fri at 4:05pm EST. A cached report is
    acceptable when no scheduled run has occurred between the cache timestamp
    and now — so Friday's report stays valid through Monday 4:05pm.
    """
    if now is None:
        now = datetime.now(tz=ZoneInfo("America/New_York"))

    age = now - report_timestamp
    # Clock-skew guard: a future-dated cache (NTP skew, manual clock change)
    # would otherwise short-circuit to "fresh forever". Reject and let the
    # next scheduled run rebuild it.
    if age.total_seconds() < 0:
        return False
    # Within 22h is always fresh — short-circuit to skip the schedule walk.
    if age.total_seconds() < 22 * 3600:
        return True
    return not _scheduled_run_occurred_between(report_timestamp, now)


def _scheduled_run_occurred_between(start: datetime, end: datetime) -> bool:
    """Did a Mon-Fri 4:05pm EST scheduled Daily Report run occur in (start, end]?"""
    if end <= start:
        return False
    cursor = start.replace(hour=16, minute=5, second=0, microsecond=0)
    if cursor <= start:
        cursor = cursor + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:  # Mon-Fri
            return True
        cursor = cursor + timedelta(days=1)
    return False


def _get_cached_compare_result(symbol):
    """
    Fast-path: return the same-day daily-report entry for this symbol if available.
    Returns a dict copy (with `cached_from_report=True` and `cached_at` metadata)
    or None if the report is stale / missing / doesn't contain the symbol.

    The daily report runs at 4:05 PM EST and stores full `predict_ticker_compare`
    results, so we can serve them instantly instead of rebuilding both models.
    Cache is valid until the next scheduled 4:05pm EST run (weekend-aware).
    """
    with _report_lock:
        gen_at = _report_cache.get("generated_at")
        data = _report_cache.get("data")

    # Cold-start fallback: in-memory cache is empty, but a valid report may exist
    # on disk. _load_latest_report_from_disk() populates _report_cache as a
    # side-effect under its own lock; re-snapshot after.
    if not gen_at or not data:
        _load_latest_report_from_disk()
        with _report_lock:
            gen_at = _report_cache.get("generated_at")
            data = _report_cache.get("data")
        if not gen_at or not data:
            return None

    try:
        gen_dt = datetime.fromisoformat(gen_at)
    except Exception:
        return None
    # Weekend-aware freshness: cache is valid until the next scheduled
    # 4:05pm EST run.
    if not _is_cached_report_acceptable(gen_dt):
        return None

    entries = data.get('data', []) if isinstance(data, dict) else data
    for entry in entries:
        if entry.get('symbol') == symbol:
            hit = dict(entry)
            hit['cached_from_report'] = True
            hit['cached_at'] = gen_at
            return hit
    return None


@app.get("/api/predict-compare/{symbol}")
async def api_predict_compare(symbol: str, strategy: str = None, refresh: bool = False):
    """
    Run BOTH Lite and Pro models on a single ticker.
    Returns side-by-side predictions with consensus signal.
    Query params:
      ?strategy=auto    — auto (ETF→full, stock→buy_only), full, buy_only
      ?refresh=true     — bypass same-day report cache, run fresh prediction
    """
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 6:
        raise HTTPException(400, "Invalid ticker symbol")

    # Fast-path: if today's daily report already contains this symbol, serve it instantly.
    # Only applies to the default auto strategy — explicit overrides need fresh compute
    # since the report was generated with auto strategy selection per symbol.
    if not refresh and (strategy is None or strategy == 'auto'):
        cached = _get_cached_compare_result(symbol)
        if cached is not None:
            best_map = _get_best_strategy_map()
            cached['best_strategy'] = best_map.get(symbol)
            _inject_classifier_fields(cached, symbol)
            return JSONResponse(cached)

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
    _inject_classifier_fields(result, symbol)
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
                _report_cache["generated_at"] = datetime.now(ZoneInfo("America/New_York")).isoformat()
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] Dual report complete — {report['summary']['total_symbols']} symbols.\n")
            # Send email alert if any high-confidence signals found
            _send_signal_alerts(report)
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
                # C-11: banner band — keep in sync with DAILY_REPORT_EST in
                # frontend/index.html and USER_GUIDE.md Parts 4 & 11.
                "message": "Dual-model report started. This may take 25-55 minutes.",
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
            # C-12: derive a parseable ISO timestamp. Prefer the embedded summary
            # value; fall back to the file's mtime (NOT the filename string,
            # which was 'dual_report_YYYYMMDD_HHMM.json' and broke Date parsing
            # on the frontend → "Invalid Date").
            gen_at = data.get('summary', {}).get('generated_at')
            # Back-compat: pre-Round 8a reports stored naive ISO timestamps.
            # Localize any naive value to America/New_York so the freshness
            # check works on non-EST machines.
            if gen_at:
                try:
                    parsed = datetime.fromisoformat(gen_at)
                    if parsed.tzinfo is None:
                        gen_at = parsed.replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
                except (ValueError, TypeError):
                    pass
            if not gen_at:
                try:
                    gen_at = datetime.fromtimestamp(
                        os.path.getmtime(latest), tz=ZoneInfo("America/New_York")
                    ).isoformat()
                except Exception:
                    gen_at = None
            with _report_lock:
                _report_cache["data"] = data
                _report_cache["generated_at"] = gen_at
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


@app.get("/api/system/status")
async def api_system_status():
    """Runtime capability flags for the dashboard. H-3: Pro (v3) silently
    renders 'Not available' when LightGBM is missing; the frontend reads
    this endpoint on load and shows a persistent banner so users know the
    Lite-vs-Pro comparison is off and how to enable it.
    """
    pro_available = bool(HAS_LGBM)
    return JSONResponse({
        "has_lgbm": pro_available,
        "model_version": "v2",
        "pro_available": pro_available,
        "notes": {
            "pro_unavailable_reason": None if pro_available else "lightgbm package not installed",
            "install_hint": None if pro_available else "pip install lightgbm",
        },
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
                'cached_date': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d'),
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


# =============================================================================
# FUNDAMENTAL SCREENER (Good Firm Framework)
# =============================================================================
# Additive only: three new endpoints (screener table, single-ticker detail,
# SEC refresh trigger). Does NOT affect any existing endpoint, scheduler, or
# cache. If the screener modules are unavailable, endpoints return 503.

# Bucket 2 (2026-04-21): the prior 6h in-memory TTL cache that recomputed
# the 85-ticker screen on miss is gone. ``verdict_provider.load_screener_index``
# is the single cache now, keyed by screener_results.csv mtime — so the
# Lookup/Report/Leader tabs can never disagree, and /api/screener/{sym}
# responses stay sub-millisecond after the first load. ``POST /api/screener/refresh``
# (SEC pull) is untouched; it's the refresh surface that still exists.

_edgar_lock = threading.Lock()
_edgar_state = {
    "is_running": False,
    "total": 0,
    "completed": 0,
    "current_symbol": None,
    "started_at": None,
    "results": None,
    "error": None,
}


def _edgar_refresh_worker(symbols):
    """Background worker: pull latest XBRL facts from SEC for each symbol."""
    with _edgar_lock:
        _edgar_state["is_running"] = True
        _edgar_state["total"] = len(symbols)
        _edgar_state["completed"] = 0
        _edgar_state["current_symbol"] = None
        _edgar_state["started_at"] = time.time()
        _edgar_state["results"] = None
        _edgar_state["error"] = None
    try:
        conn = _edgar_get_db()
        results = {'ok': 0, 'skipped': 0, 'not_found': 0, 'no_facts': 0, 'error': 0}
        from edgar_fetcher import fetch_one as _fetch_one
        for sym in symbols:
            with _edgar_lock:
                _edgar_state["current_symbol"] = sym
            try:
                r = _fetch_one(sym, conn, force=True)
                results[r] = results.get(r, 0) + 1
            except Exception as e:
                print(f"[Edgar] {sym}: {e}")
                results['error'] += 1
            with _edgar_lock:
                _edgar_state["completed"] += 1
        conn.close()
        with _edgar_lock:
            _edgar_state["results"] = results
        # Bucket 2: no in-memory screener cache to invalidate. The next
        # pipeline run will rewrite screener_results.csv and the mtime
        # change automatically invalidates verdict_provider's cache.
    except Exception as e:
        with _edgar_lock:
            _edgar_state["error"] = str(e)
    finally:
        with _edgar_lock:
            _edgar_state["is_running"] = False
            _edgar_state["current_symbol"] = None


@app.get("/api/screener")
async def api_screener(refresh: bool = False):
    """Full Good Firm screener table — served from screener_results.csv.

    The response shape is unchanged: ``{data: [...], computed_at: ISO,
    cached: bool}``. ``computed_at`` now reflects the on-disk CSV mtime
    (rather than a memory-cache write timestamp), so the UI can render
    "As of HH:MM" off it.

    ``?refresh=true`` is retained for backward compat (old clients
    occasionally pass it as a cache-buster). It no longer recomputes
    anything — it just forces a CSV re-read even when the mtime hasn't
    changed. Actual data refresh lives on ``POST /api/screener/refresh``
    (SEC pull) and the leaders rebuild pipeline.
    """
    if not HAS_SCREENER:
        raise HTTPException(503, "Screener module unavailable")
    index = _verdict_provider.load_screener_index(force_reload=refresh)
    data = list(index.values())
    computed_at = _verdict_provider.get_csv_mtime_iso()
    if not data:
        return JSONResponse({
            "data": [],
            "computed_at": computed_at,
            "cached": not refresh,
            "hint": "No fundamentals cached yet — POST /api/screener/refresh "
                    "to pull SEC data, then run the screener.",
        })
    return JSONResponse({
        "data": data,
        "computed_at": computed_at,
        "cached": not refresh,
    })


@app.get("/api/screener/{symbol}")
async def api_screener_symbol(symbol: str, refresh: bool = False):
    """Single-ticker Good Firm verdict.

    Always returns a 200 with a verdict dict — missing symbols come back
    as INSUFFICIENT_DATA carrying a ``reason`` code (NO_SEC_FILINGS,
    TAXONOMY_GAP, INSUFFICIENT_HISTORY) and matching ``reason_text`` so
    the frontend can render Sophia's human-friendly copy without a
    second round-trip."""
    if not HAS_SCREENER:
        raise HTTPException(503, "Screener module unavailable")
    symbol = symbol.upper().strip()
    if not symbol.isalnum() or len(symbol) > 6:
        raise HTTPException(400, "Invalid ticker symbol")
    if refresh:
        # Backward compat — force an mtime-cache reload. The SEC refresh
        # endpoint is POST /api/screener/refresh and is a different surface.
        _verdict_provider.load_screener_index(force_reload=True)
    payload = _verdict_provider.load_verdict_for_symbol(symbol)
    # Round 7d: surface the screener CSV mtime so the verdict card can render
    # an "As of YYYY-MM-DD" chip. Scoped to this endpoint only — the
    # /api/predict and /api/predict-compare surfaces deliberately do NOT
    # carry this field (Layer 2 ML predictions are not bound to the screener
    # CSV's mtime; surfacing it there would conflate two different data
    # freshness signals).
    payload["as_of_csv_mtime"] = _verdict_provider.get_csv_mtime_iso()
    return JSONResponse(payload)


@app.post("/api/screener/refresh")
async def api_screener_refresh():
    """Trigger a background SEC pull for all tickers in Tickers.csv.
    Returns 202 with a status URL. Use /api/screener/refresh/status to poll."""
    if not HAS_SCREENER:
        raise HTTPException(503, "Screener module unavailable")
    with _edgar_lock:
        if _edgar_state["is_running"]:
            raise HTTPException(409, "Refresh already running")
    symbols = _edgar_load_tickers()
    if not symbols:
        raise HTTPException(500, "Tickers.csv is empty or missing")
    t = threading.Thread(
        target=_edgar_refresh_worker, args=(symbols,), daemon=True
    )
    t.start()
    return JSONResponse({
        "status": "started",
        "total": len(symbols),
        "status_url": "/api/screener/refresh/status",
    }, status_code=202)


@app.get("/api/screener/refresh/status")
async def api_screener_refresh_status():
    """Poll the SEC-refresh background job."""
    if not HAS_SCREENER:
        raise HTTPException(503, "Screener module unavailable")
    with _edgar_lock:
        snap = dict(_edgar_state)
    elapsed = round(time.time() - snap["started_at"]) if snap["started_at"] else 0
    return JSONResponse({
        "status": "running" if snap["is_running"] else "done",
        "total": snap["total"],
        "completed": snap["completed"],
        "current_symbol": snap["current_symbol"],
        "elapsed_seconds": elapsed,
        "results": snap["results"],
        "error": snap["error"],
    })


# =============================================================================
# LEADER DETECTOR (Layer 1 — leaders.csv / universe.csv / rebuild trigger)
# =============================================================================
# Four endpoints, all additive — failure here does NOT affect Layer 2:
#   GET  /api/leaders                 → current leaders.csv as JSON (Phase 1.4 out)
#   GET  /api/universe?source=...     → ranked 500 / prescreened / raw universe
#   POST /api/leaders/rebuild         → kick off full Phase 1.0 → 1.4 pipeline
#   GET  /api/leaders/rebuild/status  → poll background rebuild

_LEADERS_DIR = os.path.dirname(os.path.abspath(__file__))
_LEADERS_CSV_PATH          = os.path.join(_LEADERS_DIR, "leaders.csv")
_SCREENER_RESULTS_PATH     = os.path.join(_LEADERS_DIR, "screener_results.csv")
_UNIVERSE_PRESCREENED_PATH = os.path.join(_LEADERS_DIR, "universe_prescreened.csv")
_UNIVERSE_RAW_PATH         = os.path.join(_LEADERS_DIR, "universe_raw.csv")

_leaders_lock = threading.Lock()
_leaders_state = {
    "is_running": False,
    "stage": None,          # 'universe_build' | 'sec_fetch' | 'screener' | 'leader_select'
    "started_at": None,
    "finished_at": None,
    "result": None,         # 'ok' on clean completion
    "error": None,
    "last_stdout_tail": None,
}


def _read_csv_as_list_of_dicts(path):
    """Read a CSV as list[dict[str,str]]. Returns [] if missing/empty/unreadable."""
    if not os.path.exists(path):
        return []
    try:
        if os.path.getsize(path) == 0:
            return []
    except OSError:
        return []
    try:
        import csv
        with open(path, 'r', newline='', encoding='utf-8') as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception as exc:
        print(f"[Leaders] Failed to read {path}: {exc}")
        return []


def _file_mtime_iso(path):
    """Return file mtime as ISO string, or None if missing."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
    except OSError:
        return None


def _leaders_rebuild_worker():
    """Run the Layer 1 pipeline end-to-end as subprocess stages.
    Each stage is its own process so a crash in (say) SEC fetch doesn't tank
    the FastAPI process. Progress is per-stage, not per-ticker."""
    import subprocess
    import sys
    python_exe = sys.executable or "python"

    stages = [
        # Phases 1.0 + 1.1 (--build runs both): ~130 min cold, <1 min warm (checkpointed)
        ("universe_build", [python_exe, "universe_builder.py", "--build"]),
        # Phase 1.2: ~85 min cold for ~500 tickers; ~seconds warm (90-day XBRL TTL)
        ("sec_fetch", [python_exe, "edgar_fetcher.py",
                       "--universe", "universe_prescreened.csv"]),
        # Phase 1.3: ~5 min — reads SQLite, writes flat CSV
        ("screener", [python_exe, "fundamental_screener.py",
                      "--universe", "universe_prescreened.csv",
                      "--csv-out", "screener_results.csv"]),
        # Phase 1.4: ~1 s — pure-local selection
        ("leader_select", [python_exe, "leader_selector.py", "--build"]),
    ]

    with _leaders_lock:
        _leaders_state["is_running"] = True
        _leaders_state["started_at"] = time.time()
        _leaders_state["finished_at"] = None
        _leaders_state["stage"] = None
        _leaders_state["result"] = None
        _leaders_state["error"] = None
        _leaders_state["last_stdout_tail"] = None

    try:
        for name, cmd in stages:
            with _leaders_lock:
                _leaders_state["stage"] = name
            print(f"[Leaders] Stage '{name}' starting: {' '.join(cmd)}")
            # 3-hour per-stage timeout — universe_build can legitimately run ~130 min cold.
            r = subprocess.run(
                cmd, cwd=_LEADERS_DIR,
                capture_output=True, text=True, timeout=10800,
            )
            tail = (r.stdout or "")[-500:] + "\n" + (r.stderr or "")[-500:]
            with _leaders_lock:
                _leaders_state["last_stdout_tail"] = tail
            if r.returncode != 0:
                raise RuntimeError(
                    f"Stage '{name}' failed (rc={r.returncode}). Tail:\n{tail}"
                )
            print(f"[Leaders] Stage '{name}' OK")
        with _leaders_lock:
            _leaders_state["result"] = "ok"
        print("[Leaders] Rebuild complete.")
    except Exception as exc:
        print(f"[Leaders] Rebuild failed: {exc}")
        with _leaders_lock:
            _leaders_state["error"] = str(exc)
    finally:
        with _leaders_lock:
            _leaders_state["is_running"] = False
            _leaders_state["finished_at"] = time.time()
            _leaders_state["stage"] = None


@app.get("/api/leaders")
async def api_leaders():
    """Current leaders.csv (Phase 1.4 output) as JSON.

    Feeds the Leader Detector UI's "selected leaders" view. Returns an empty
    list with a hint if Layer 1 hasn't run yet."""
    data = _read_csv_as_list_of_dicts(_LEADERS_CSV_PATH)
    if not data:
        return JSONResponse({
            "data": [],
            "count": 0,
            "last_modified": None,
            "source": "leaders.csv",
            "hint": "leaders.csv not found or empty. "
                    "POST /api/leaders/rebuild to generate.",
        })
    return JSONResponse({
        "data": data,
        "count": len(data),
        "last_modified": _file_mtime_iso(_LEADERS_CSV_PATH),
        "source": "leaders.csv",
    })


@app.get("/api/universe")
async def api_universe(source: str = "screener"):
    """Full Layer 1 universe snapshots.

    source=screener    (default) → screener_results.csv (~500 scored, 29 cols)
    source=prescreened           → universe_prescreened.csv (Phase 1.1 output)
    source=raw                   → universe_raw.csv (Phase 1.0 output, ~1400 rows)
    """
    path_map = {
        "screener":    _SCREENER_RESULTS_PATH,
        "prescreened": _UNIVERSE_PRESCREENED_PATH,
        "raw":         _UNIVERSE_RAW_PATH,
    }
    path = path_map.get(source)
    if not path:
        raise HTTPException(
            400, f"Unknown source '{source}'. Valid: {list(path_map)}"
        )
    data = _read_csv_as_list_of_dicts(path)
    return JSONResponse({
        "data": data,
        "count": len(data),
        "source": os.path.basename(path),
        "last_modified": _file_mtime_iso(path),
    })


@app.post("/api/leaders/rebuild")
async def api_leaders_rebuild():
    """Trigger a full Phase 1.0 → 1.4 rebuild in the background.
    Returns 202 immediately. Poll /api/leaders/rebuild/status for progress.

    Cold ETAs (per stage):
      universe_build  Phases 1.0+1.1  ~130 min
      sec_fetch       Phase 1.2       ~85 min
      screener        Phase 1.3       ~5 min
      leader_select   Phase 1.4       ~1 s
    Total cold ~3.5 hours. Warm reruns (checkpointed + 90-day XBRL TTL) ~10 min.
    """
    with _leaders_lock:
        if _leaders_state["is_running"]:
            raise HTTPException(
                409, f"Rebuild already running (stage={_leaders_state['stage']})"
            )
    t = threading.Thread(target=_leaders_rebuild_worker, daemon=True)
    t.start()
    return JSONResponse({
        "status": "started",
        "status_url": "/api/leaders/rebuild/status",
        "estimated_minutes_cold": 215,
        "estimated_minutes_warm": 10,
    }, status_code=202)


@app.get("/api/leaders/rebuild/status")
async def api_leaders_rebuild_status():
    """Poll the background rebuild job.
    Status transitions: idle → running (with stage) → done | error
    """
    with _leaders_lock:
        snap = dict(_leaders_state)
    if snap["is_running"]:
        status = "running"
    elif snap["error"]:
        status = "error"
    elif snap["result"] == "ok":
        status = "done"
    else:
        status = "idle"
    started, finished = snap["started_at"], snap["finished_at"]
    if snap["is_running"] and started:
        elapsed = round(time.time() - started)
    elif started and finished:
        elapsed = round(finished - started)
    else:
        elapsed = 0
    return JSONResponse({
        "status": status,
        "stage": snap["stage"],
        "elapsed_seconds": elapsed,
        "result": snap["result"],
        "error": snap["error"],
        "last_stdout_tail": snap["last_stdout_tail"],
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
