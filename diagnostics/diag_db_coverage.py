"""Phase 1.9b — probe fundamentals.db coverage against the current
prescreened universe.

Question this script answers:
  Did Phase 1.7f re-fetch (the ~200-min ingest over universe_prescreened.csv)
  finish? OR does the DB still reflect the old 85-row Tickers.csv universe?

Why it matters:
  The 4-verdict rubric (Phase 1.9) output today showed exactly 85 tickers
  with verdicts — matches Tickers.csv row count, not the 1,414 prescreened
  pool. Until we know whether the DB was populated for the real universe,
  any rubric tuning / CostOfServices patch / leader_selector run is waste.

Output:
  - Total distinct symbols in `facts` table
  - Total rows in `tickers` table
  - Sizes of universe_prescreened.csv and Tickers.csv
  - Intersection counts DB ∩ prescreened / DB ∩ Tickers.csv
  - How many prescreened tickers are MISSING from the DB (the ingest gap)
  - Quick spot-check: does the DB have NVDA/MSFT (both universes) and
    TMDX/ATI/TVTX (prescreened-only, not in Tickers.csv)?

Read-only, zero HTTP. Runs in ~1 sec.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / 'fundamentals.db'
PRESCREENED_CSV = ROOT / 'universe_prescreened.csv'
TICKERS_CSV = ROOT / 'Tickers.csv'

# Spot-check names: picked because TMDX / ATI / TVTX / HUBB are in the
# prescreened pool but NOT in the legacy Tickers.csv (so they'd only be in
# the DB if Phase 1.7f actually ran). NVDA / MSFT / AAPL are in both,
# so seeing them in the DB is inconclusive on its own.
SPOT_BOTH = ['NVDA', 'MSFT', 'AAPL']
SPOT_PRESCREENED_ONLY = ['TMDX', 'ATI', 'TVTX', 'HUBB', 'ADSK']


def _read_symbols_from_csv(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        # Auto-detect the symbol column: "symbol" is canonical; some
        # Tickers.csv vintages use "ticker" or "Symbol"
        if not reader.fieldnames:
            return set()
        sym_col = next(
            (c for c in reader.fieldnames if c.lower() in ('symbol', 'ticker')),
            reader.fieldnames[0],
        )
        return {(r.get(sym_col) or '').strip().upper() for r in reader}


def main():
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        raise SystemExit(1)

    conn = sqlite3.connect(str(DB_PATH))
    db_syms = {r[0].upper() for r in conn.execute(
        "SELECT DISTINCT symbol FROM facts"
    ) if r[0]}
    ticker_rows = conn.execute("SELECT COUNT(*) FROM tickers").fetchone()[0]

    pre_syms = _read_symbols_from_csv(PRESCREENED_CSV) - {''}
    tic_syms = _read_symbols_from_csv(TICKERS_CSV) - {''}

    print('=' * 72)
    print('  fundamentals.db coverage probe')
    print('=' * 72)
    print(f"  DB distinct symbols in `facts`:  {len(db_syms):>6,}")
    print(f"  DB rows in `tickers` table:      {ticker_rows:>6,}")
    print(f"  universe_prescreened.csv:        {len(pre_syms):>6,}")
    print(f"  Tickers.csv:                     {len(tic_syms):>6,}")
    print()
    print(f"  DB ∩ prescreened:                {len(db_syms & pre_syms):>6,}")
    print(f"  DB ∩ Tickers.csv:                {len(db_syms & tic_syms):>6,}")
    print(f"  prescreened NOT in DB (gap):     "
          f"{len(pre_syms - db_syms):>6,}")
    print()

    print('  Spot check — tickers in BOTH universes (inconclusive alone):')
    for sym in SPOT_BOTH:
        mark = '✓' if sym in db_syms else '✗'
        print(f"    {mark} {sym}")
    print()

    print('  Spot check — tickers ONLY in prescreened '
          '(proof Phase 1.7f ran):')
    for sym in SPOT_PRESCREENED_ONLY:
        mark = '✓' if sym in db_syms else '✗'
        print(f"    {mark} {sym}")
    print()

    # Verdict
    print('=' * 72)
    print('  Diagnosis')
    print('=' * 72)
    ratio_pre = len(db_syms & pre_syms) / len(pre_syms) if pre_syms else 0
    if ratio_pre >= 0.95:
        print(f"  ✅ Phase 1.7f re-fetch FINISHED — "
              f"{ratio_pre:.0%} of prescreened universe in DB.")
        print(f"     Next: python fundamental_screener.py --all "
              f"(re-verdict against 1,414 pool)")
    elif ratio_pre >= 0.10:
        print(f"  ⚠ Phase 1.7f PARTIALLY complete — "
              f"{ratio_pre:.0%} of prescreened in DB "
              f"({len(pre_syms - db_syms):,} missing).")
        print(f"     Next: edgar_fetcher.py --universe "
              f"universe_prescreened.csv --refresh (close the gap).")
    else:
        print(f"  ✗ Phase 1.7f DID NOT RUN against prescreened pool "
              f"(only {ratio_pre:.0%} overlap).")
        print(f"     DB still reflects legacy Tickers.csv "
              f"(∩={len(db_syms & tic_syms)}).")
        print(f"     Next: python edgar_fetcher.py --universe "
              f"universe_prescreened.csv --refresh  # ~200 min cold")
    conn.close()


if __name__ == '__main__':
    main()
