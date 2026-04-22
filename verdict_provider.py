"""
verdict_provider.py
───────────────────
Unified reader for the fundamental-verdict shape shared across all three
tabs (Lookup compare card, Daily Report drill-down, Leader Detector
click-through). Backend of record: ``screener_results.csv``.

Design
------
Layer 1's ``fundamental_screener.py --csv-out screener_results.csv`` is the
canonical source. Before Bucket 2, the FastAPI layer held its own 6h TTL
cache that recomputed the 85-ticker screen on miss, while the Leader
Detector read the on-disk CSV directly — so the two tabs could disagree
for up to 6 hours after a rebuild, and the Daily Report tab hit a third
code path. This module replaces all three with a single mtime-keyed
in-process cache that re-reads the CSV only when its mtime changes.

Reason codes (INSUFFICIENT_DATA split)
--------------------------------------
Every INSUFFICIENT_DATA row now carries a ``reason`` + ``reason_text`` so
the UI can distinguish:

* ``NO_SEC_FILINGS``      — symbol not in screener_results.csv at all
                            (ETF, ADR, foreign issuer, untracked).
* ``TAXONOMY_GAP``        — symbol IS in CSV but ``archetype == 'UNKNOWN'``
                            (filings exist but our parser can't read the
                            revenue concept — CRWD/APA et al.).
* ``INSUFFICIENT_HISTORY`` — symbol in CSV, archetype known, but
                            ``tests_known < 3`` (not enough reported
                            quarters yet).

Public API
----------
    load_screener_index()           -> dict[symbol -> row]  (mtime-cached)
    load_verdict_for_symbol(sym)    -> dict in the shape consumed by
                                       frontend buildVerdictCard()
    classify_reason(row, in_index)  -> ('reason', 'reason_text') tuple
    get_csv_mtime_iso()             -> ISO string of CSV mtime (for
                                       surfacing "As of …" in the UI)
    csv_has_required_columns()      -> (bool, missing_columns: list[str])
                                       used at startup to warn if the
                                       CSV predates the Bucket 2 schema.
"""
from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime
from typing import Any

# Canonical location — matches api_server._SCREENER_RESULTS_PATH. Kept as
# a module-level default rather than import-time resolution so tests can
# monkeypatch if needed.
SCREENER_CSV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "screener_results.csv"
)

# Columns that were added in Bucket 2. Their absence on legacy CSVs is
# non-fatal — we just emit a one-time startup warning and the verdict card
# falls back to dashes / empty chip rows.
REQUIRED_BUCKET2_COLUMNS = ("tests_json", "dealbreakers_json")

# Reason-code copy (Sophia's locked wording, 2026-04-21). Kept out of the
# frontend so backend consumers (email alerts, future Slack notifications)
# stay consistent.
_REASON_TEXT = {
    "NO_SEC_FILINGS": (
        "We don't have SEC filings for this symbol — likely an ETF, ADR, "
        "or non-US issuer. Fundamental verdict doesn't apply here."
    ),
    "TAXONOMY_GAP": (
        "This company files with the SEC, but reports revenue in a "
        "non-standard format our parser can't read yet. We're tracking "
        "it — no verdict for now."
    ),
    "INSUFFICIENT_HISTORY": (
        "Not enough reported quarters yet to grade fundamentals (we need "
        "~3 years of filings). Check back after the next earnings cycle."
    ),
}

# Columns we coerce to float when present (empty string -> None).
_FLOAT_COLS = (
    "market_cap", "dividend_yield",
    "good_firm_score",
    "revenue_yoy_growth", "revenue_3y_cagr",
    "gross_margin_ttm", "operating_margin_ttm",
    "operating_cash_flow_ttm", "free_cash_flow_ttm",
    "fcf_margin_ttm", "rule_40_score",
    "roic_ttm", "svr", "svr_vs_sector_median",
)

_INT_COLS = (
    "tests_passed", "tests_known",
    "market_cap_rank_in_sector", "sector_peers",
)

_BOOL_COLS = (
    "flag_diluting", "flag_burning_cash", "flag_spac_or_microcap",
)

# ─── Process-lifetime cache, keyed by (path, mtime_ns) ───────────────────
_cache_lock = threading.Lock()
_cache_state: dict[str, Any] = {
    "path": None,
    "mtime_ns": None,
    "index": None,       # dict[str, dict]
    "column_set": None,  # frozenset of header columns
}


def _coerce_row(raw: dict[str, str]) -> dict[str, Any]:
    """Narrow CSV strings to the types the frontend expects.

    The CSV is authoritative but everything is string-typed on read;
    ``fmtPct`` / ``pctHTML`` / ``testDot`` in the frontend all assume
    numbers-or-None and booleans-or-None, so we normalize here once
    rather than scatter ``parseFloat`` calls through the UI.
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if v is None or v == "":
            out[k] = None
            continue
        out[k] = v

    for c in _FLOAT_COLS:
        v = out.get(c)
        if v is None:
            continue
        try:
            out[c] = float(v)
        except (TypeError, ValueError):
            out[c] = None

    for c in _INT_COLS:
        v = out.get(c)
        if v is None:
            continue
        try:
            out[c] = int(float(v))
        except (TypeError, ValueError):
            out[c] = None

    for c in _BOOL_COLS:
        v = out.get(c)
        if v is None:
            continue
        # write_screener_csv emits 1/0 for booleans; preserve that mapping.
        out[c] = v in ("1", "true", "True", True, 1)

    # Deserialize the JSON blobs (Bucket 2 additions). Missing = dashes in UI.
    tests_raw = out.pop("tests_json", None)
    if tests_raw:
        try:
            out["tests"] = json.loads(tests_raw)
        except (TypeError, ValueError):
            out["tests"] = {}
    else:
        out["tests"] = {}

    db_raw = out.pop("dealbreakers_json", None)
    if db_raw:
        try:
            out["dealbreakers"] = json.loads(db_raw)
        except (TypeError, ValueError):
            out["dealbreakers"] = {}
    else:
        out["dealbreakers"] = {}

    # Keep symbol uppercase — the rest of the stack expects it.
    if out.get("symbol"):
        out["symbol"] = out["symbol"].upper()

    return out


def _read_csv(path: str) -> tuple[dict[str, dict], frozenset[str]]:
    """Parse the CSV into a symbol -> row index."""
    index: dict[str, dict] = {}
    columns: frozenset[str] = frozenset()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return index, columns
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = frozenset(reader.fieldnames or ())
        for raw in reader:
            sym = (raw.get("symbol") or "").strip().upper()
            if not sym:
                continue
            index[sym] = _coerce_row(raw)
    return index, columns


def _mtime_ns(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def load_screener_index(
    path: str | None = None, force_reload: bool = False
) -> dict[str, dict]:
    """Return the symbol -> row index, reloading only when the CSV's
    mtime changes. ``force_reload=True`` forces a re-read regardless
    (used by the ``?refresh=true`` cache-buster on /api/screener)."""
    path = path or SCREENER_CSV_PATH
    current_mtime = _mtime_ns(path)

    with _cache_lock:
        if (
            not force_reload
            and _cache_state["index"] is not None
            and _cache_state["path"] == path
            and _cache_state["mtime_ns"] == current_mtime
        ):
            return _cache_state["index"]

    # Read outside the lock — the file can be large-ish (~400 KB, 1.4k rows)
    # and we don't want concurrent /api/screener requests to serialize.
    index, columns = _read_csv(path)

    with _cache_lock:
        _cache_state["path"] = path
        _cache_state["mtime_ns"] = current_mtime
        _cache_state["index"] = index
        _cache_state["column_set"] = columns

    return index


def classify_reason(
    row: dict | None, in_index: bool
) -> tuple[str, str]:
    """Pick the right INSUFFICIENT_DATA reason for a symbol.

    Kept here rather than inlined so that the three-way split (index
    membership, archetype, tests_known) stays in one place. Wright's
    review flagged this as load-bearing — getting it wrong on any of
    the CRWD/APA-style TAXONOMY_GAP cases re-creates the original bug.
    """
    if not in_index:
        return "NO_SEC_FILINGS", _REASON_TEXT["NO_SEC_FILINGS"]

    archetype = (row or {}).get("archetype") or ""
    if archetype == "UNKNOWN":
        return "TAXONOMY_GAP", _REASON_TEXT["TAXONOMY_GAP"]

    # Archetype known but not enough quarters surfaced (tests_known < 3).
    # This is the newly listed / recent IPO case — valid company, just
    # not enough history yet.
    return "INSUFFICIENT_HISTORY", _REASON_TEXT["INSUFFICIENT_HISTORY"]


def load_verdict_for_symbol(symbol: str) -> dict:
    """Return the full verdict shape for a single ticker, always a dict.

    Callers: /api/screener/{symbol} and (indirectly) the frontend
    Lookup/Report/Leader paths. Never raises — missing symbols return an
    INSUFFICIENT_DATA shape with a reason code so the UI can render
    Sophia's human-friendly copy without any extra lookup.
    """
    sym = (symbol or "").strip().upper()
    index = load_screener_index()
    row = index.get(sym)

    if row and row.get("verdict") and row.get("verdict") != "INSUFFICIENT_DATA":
        # Happy path — row is already a full verdict dict.
        return row

    reason, reason_text = classify_reason(row, in_index=row is not None)
    # Start from the row so any metric fields we DO have (name, sector,
    # market_cap on an UNKNOWN archetype row) still flow to the card.
    out: dict[str, Any] = dict(row) if row else {"symbol": sym}
    out["symbol"] = sym
    out["verdict"] = "INSUFFICIENT_DATA"
    out["reason"] = reason
    out["reason_text"] = reason_text
    return out


def get_csv_mtime_iso(path: str | None = None) -> str | None:
    """ISO timestamp of the CSV's mtime — surfaced in /api/screener's
    top-level ``computed_at`` so the UI can render 'As of YYYY-MM-DD HH:MM'.
    Returns None if the file is missing."""
    path = path or SCREENER_CSV_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return datetime.fromtimestamp(mtime).isoformat()


def csv_has_required_columns(
    path: str | None = None,
) -> tuple[bool, list[str]]:
    """Check if the on-disk CSV has the Bucket 2 columns. Used by the
    startup warning in api_server — missing columns are non-fatal,
    but the test-dot row / flag chips silently render dashes until the
    next screener run."""
    path = path or SCREENER_CSV_PATH
    if not os.path.exists(path):
        return True, []  # nothing to check; startup log elsewhere covers this
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
    except OSError:
        return True, []
    header_set = set(header)
    missing = [c for c in REQUIRED_BUCKET2_COLUMNS if c not in header_set]
    return (not missing), missing
