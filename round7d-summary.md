# Round 7d — implementation summary

Branch: `agent-round7d`, six feature/test/fix commits ahead of `3c33c50` (the last Round 7c-2 docs commit) plus this docs commit. Not pushed, not merged. Three-agent team: **wright** reviewed the schema and backend wiring across Phase 1 and Phase 2, **skipper** implemented all six commits, **sophia** reviewed the frontend in Phase 3 and requested two refinements that landed as the fix-commit.

Two feedback items closed: **FB-1 (display half)** — peer-median benchmarking surfaced on the verdict card alongside each company value — and **FB-4** — the rolled-up "data freshness chip + ETF P/E tooltip + verdict-card layout refactor" cluster carried over from Round 7c-2's deferred list.

The data half of FB-1 (the canonical sector / industry_group / industry classifier) shipped in Round 7c. This round wires the peer-median values into the screener pipeline, the API response, and the verdict card itself, completing the FB-1 arc.

---

## What shipped

| Commit | Title |
|---|---|
| `e3555a2` | feat: precompute peer_median columns for 8 metrics in screener_results.csv |
| `250aa07` | feat: peer median fields flow through verdict_provider and API responses |
| `301b94a` | test: peer median computation unit tests (6 tests) |
| `0076b0e` | feat: verdict card three-column grid layout with peer median + drop svr_vs_sector_median |
| `10be25b` | feat: timestamp chip on verdict card showing data freshness; ETF P/E tooltip |
| `a1ae043` | fix: ETF peer-comparison inline note + raw-ISO chip tooltip per sophia review |
| _(this commit)_ | docs: Round 7d summary, schema notes, CHANGELOG, FEATURE_BACKLOG updates |

57 tests pass after each commit (51 prior + 6 new in `test_peer_median.py`). Tree clean. No push, no merge — gating is the owner's CSV-regen + UI spot-check (see "Owner verification" below).

**CSV regeneration required post-merge.** Commits `e3555a2` and `0076b0e` change the schema of `screener_results.csv`: 9 columns added (`peer_median_*` × 8 + `peer_count`), 1 column dropped (`svr_vs_sector_median`). Until the owner re-runs the screener, every verdict card will show em-dash in the Peer Median column for every metric, and the dropped score-bonus from `score_ticker` won't reflect on `leaders.csv` either. This is intentional per the round prompt — code lands first, CSV regen is the owner's verification step.

---

## Phase 1 — wright's design review (no code)

Skipper drafted a one-page design covering: (a) where peer-median computation lives (screener-side, baked into `screener_results.csv`, vs API-side at request time), (b) bucket key (industry_group vs SIC-2 vs sector), (c) min-peers threshold for "compute the median" vs "emit None", and (d) the score-bonus drift introduced by removing `svr_vs_sector_median`.

**Wright's accepted refinements:**

1. **Compute peer medians at screen time, not at request time.** The screener already runs as a batch over the full universe; computing per-bucket medians there is one extra pass over the in-memory list and adds ~50 ms to a 25–55 minute screen. API-time computation would require either a per-request scan of `screener_results.csv` (cache-miss expensive) or a second cached aggregate that has its own staleness story. Schema-side wins on simplicity — a future cache invalidation already covers it via `verdict_provider`'s mtime check.
2. **Bucket by industry_group, not SIC-2 or sector.** Round 7c's classifier guarantees each of the 29 industry groups has ≥5 members (per `classifier.py:11`'s merge of "Interactive Media & Services" into "Telecom & Media"). Sector is too coarse (Semiconductors vs all of Technology); SIC-2 is finer than the classifier's design intent and reintroduces taxonomy gaps. Industry_group hits the design target.
3. **Min-peers = 5.** Below 5 non-null values, emit `None` rather than a misleading 1-of-2 median. Wright's call: "a 2-row median is statistical noise; a 5-row median is borderline but defensible." The same threshold guards against outlier sensitivity in the small-bucket tail.
4. **Add `peer_count` column.** Independent of per-metric coverage — surfaces the bucket size so a future verdict card can render "n=12" tooltips next to the peer median (deferred to a future round). Forward-compat at zero ongoing cost.

**Wright's parked concern:** the dropped `svr_vs_sector_median` field had a `+5` score bonus in `score_ticker` (`if svr_vs_sector_median is not None and svr_vs_sector_median <= 1.0: score += 5`). Removing the field also removed the bonus. Net effect: a maximum ±5 score drift per ticker, which can flip a ticker between WATCH/CONSIDER and LEADER/GEM verdict bands. Wright accepted the drift on the grounds that the SVR-vs-SIC-2 ratio was conceptually superseded by the SVR-vs-industry-group peer median, and the bonus had been an indirect way of expressing the same comparison. Owner regenerates `leaders.csv` post-deploy and diffs to quantify churn.

---

## Phase 2 — backend wiring (commits `e3555a2`, `250aa07`, `301b94a`)

### `e3555a2` — `apply_peer_medians` in `fundamental_screener.py`

New module-level constant `PEER_MEDIAN_METRICS = (revenue_yoy_growth, revenue_3y_cagr, gross_margin_ttm, operating_margin_ttm, fcf_margin_ttm, rule_40_score, roic_ttm, svr)` — eight metrics chosen because each has a numeric value, a comparable cross-company semantic, and a verdict-card row that benefits from "vs peers" context.

New function `apply_peer_medians(scored, min_peers=5)`. Buckets the `scored` list-of-dicts by `industry_group`, computes the median over non-null values per metric, writes `peer_median_{metric}` back to every row in the bucket. Rows with no `industry_group` (ETFs, classifier-Unknown) get neither `peer_median_*` nor `peer_count` written — they stay absent, so `csv.DictWriter` emits empty strings, which the verdict card renders as em-dash.

Wired into `run_full_screen` after `score_ticker`, so per-row classifier fields are populated before bucketing. `CSV_OUT_FIELDS` grows by 9 columns (8 `peer_median_*` + `peer_count`) and shrinks by 1 (`svr_vs_sector_median` dropped). `score_ticker`'s `+5` bonus from the SVR-vs-SIC-2 ratio is removed in the same commit — comment annotates the supersession.

**Score-bonus drift.** Maximum ±5 score per ticker. Owner regenerates `leaders.csv` post-deploy and diffs to quantify churn.

### `250aa07` — flow-through in `verdict_provider.py` and `api_server.py`

`verdict_provider._FLOAT_COLS` extended with the 8 `peer_median_*` columns (so the CSV reader coerces them from strings to floats); `_INT_COLS` extended with `peer_count`. `_coerce_row` is unchanged — the whitelist is what drives type handling, so the new columns get correct types without touching the function body.

`load_verdict_for_symbol` already returns the full row dict, so `peer_median_*` and `peer_count` flow through to the API caller automatically. New `as_of_csv_mtime` field added to the verdict response (ISO-8601 UTC string from `Path(csv).stat().st_mtime`) for the timestamp chip in Phase 3 — shared with the existing `_screenerComputedAt` snapshot but surfaced per-verdict so the chip renders inside the card itself.

`api_server.py` has no change — the verdict response is already a passthrough dict.

**Wright's TZ-naive nit, parked.** `as_of_csv_mtime` is computed via `datetime.fromtimestamp(mtime).isoformat()` — naive, assumes the host is in `America/New_York` (currently true on the owner's Windows machine). Same shape as the latent timezone bug already documented in `DEVELOPMENT.md §6` for `_run_dual_report`. Will fold into the same fix when non-EST deployment becomes a thing.

### `301b94a` — 6 unit tests in `tests/unit/test_peer_median.py`

Plain-assert style, matching the rest of `tests/unit/`. Wired into `tests/unit/run_all.py` as a 6th test module.

| Test | What it guards |
|---|---|
| `test_peer_median_basic_aggregation` | 6 rows in one bucket → correct median, correct `peer_count`, other metrics None when not provided |
| `test_peer_median_below_min_threshold_returns_none` | 4 rows in a bucket (< min_peers=5) → all peer_medians None, `peer_count=4` still written |
| `test_peer_median_excludes_nones_from_count` | 6 rows, 3 with non-null SVR → SVR median None (3 < 5), other metrics with ≥5 non-nulls compute correctly |
| `test_peer_median_handles_missing_industry_group` | ETF rows with None / empty industry_group → no `peer_*` columns written, no exception |
| `test_peer_median_isolates_per_industry_group` | Two buckets with very different distributions → no leakage between buckets |
| `test_peer_median_csv_roundtrip` | `write_screener_csv` → `verdict_provider.load_screener_index` round-trip preserves floats and the new int. Guards against "added column to writer but forgot to register in `_FLOAT_COLS` / `_INT_COLS`" |

`python tests/unit/run_all.py` reports `TOTAL FAILURES: 0` across 57 tests after this commit.

**Wright's Phase 2 verdict:** LGTM with the TZ-naive nit parked. No second cycle needed.

---

## Phase 3 — frontend (commits `0076b0e`, `10be25b`)

### `0076b0e` — three-column grid + drop svr_vs_sector_median

Verdict card body refactored from the previous two-column flex layout (label | value) to a three-column CSS Grid (`grid-template-columns: 1.4fr 1fr 1fr`, `column-gap: 10px`). Header row added above the body with column titles "Metric / Company / Peer Median". The grid handles em-dash alignment cleanly: rows where peer median doesn't apply (Sector, Industry Group, Industry, Sector Rank, etc.) emit em-dash in the third column without collapsing visual rhythm — flex would have required per-row `min-width` hacks.

`rows[]` definition rewritten as `[label, value, peerKey]` triples. `peerKey` names the `peer_median_*` field on the verdict payload (without the prefix); `peerKey === null` marks a categorical / non-comparable row. Peer-column formatter `fmtPeerVal(peerKey)` matches the company-value formatter for that row: `svr` → `Nx`, `rule_40_score` → bare decimal, all others → `fmtPct`.

The pre-existing Compare-card SVR row (the one driven by `svr_vs_sector_median`) was already gone after Round 7c-2's redesign, so no compare-card edits were needed — only the verdict-card SVR-vs-sector reference inside `rows[]` was replaced, with peer-median SVR taking the same comparison slot.

`fcf_margin_ttm` added to `rows[]` per Round 7d owner approval — the backend has carried the field for multiple rounds, but the verdict card never surfaced it. Adding it with peer comparison was the natural moment.

### `10be25b` — timestamp chip + ETF P/E tooltip

**Timestamp chip.** `asOfChipHTML` rendered above the SCORE box on the verdict card, sourced from the new `v.as_of_csv_mtime` field. Reuses the existing `.as-of-chip` CSS class shared with the Daily Report Firm Score chip and the Leader Detector VERDICT column header chip — visual consistency across all three freshness signals. Hidden when the backend doesn't surface the field (older cached responses, or the INSUFFICIENT_DATA branch which returns earlier).

**ETF P/E tooltip.** `peIsETF` const added to the Compare-card P/E render branches (both populated-SVR and empty-SVR paths). When true, the P/E `<div class="value">` carries `title="Weighted average of holdings"`. Mouse-hover tooltip — explains why an ETF shows a non-zero P/E without consuming any visual real estate. Resolves the Round 7c-2 deferred item.

### Sophia review — request changes

Sophia reviewed after `10be25b` and approved the structural moves with two refinements requested before docs:

1. **ETF peer-median column should be visibly inert, not just empty.** The grid renders em-dash in the Peer Median column for every row when the ticker is an ETF (because `industry_group` is missing → no peer median computed). Sophia argued that a column of em-dashes reads like "data is broken" rather than "concept doesn't apply." Add a single inline note below the grid: "Peer median comparison not applicable for ETFs." Detect via the ETF set (NOT classifier "Unknown" — they diverge on edge cases like BRK.B).
2. **The timestamp chip's `title` tooltip should show the raw ISO string, not the formatted label.** The chip displays a humanized "as of HH:MM" via `fmtAsOf`, but mouse-hover should reveal the underlying `as_of_csv_mtime` value verbatim — useful for debugging stale-cache complaints. Currently the `title` attribute was unset.

### Phase 3 fix — `a1ae043`

Both refinements landed as a single fix commit:

- ETF inline note: `isETF` const detects via `window._etfTickersSet` membership (loaded async on app init from `/api/etfs`). When true, an additional `<div style="font-size:10px;color:var(--text-faint);padding:6px 10px;text-align:right">Peer median comparison not applicable for ETFs.</div>` is appended below the grid. The peer-column formatter also short-circuits to em-dash when `isETF` is true, so the column is consistent regardless of whether the ETF happens to share an `industry_group` with non-ETF rows (it doesn't, but the guard is cheap).
- Chip tooltip: `title="${escapeHTML(v.as_of_csv_mtime || '')}"` added to the `.as-of-chip` span on the verdict card. Hover reveals the raw ISO string.

`isETF` reuses the same Round 7c-2 `_etfTickersSet` cache mechanism rather than spinning up a parallel one. If the set hasn't resolved before the first lookup, `fmtPeerVal` falls back to numeric peer rendering rather than mis-blanking — a defensible degradation since the next render after the set resolves will correct it.

**Sophia review #2:** LGTM. Inline note reads as informational, timestamp tooltip surfaces the raw value, no further refinements requested.

---

## Phase 4 — docs (this commit)

- `round7d-summary.md` — this file.
- `DEVELOPMENT.md` — §2 schema note for the 9 new columns + 1 dropped; §6 caching note that the mtime invalidation handles the new columns automatically.
- `CHANGELOG.md` — Round 7d entry at top.
- `FEATURE_BACKLOG.md` — FB-1 (display half) and FB-4 marked shipped under Round 7d.
- `PATTERNS.md` — no new patterns rose to "reusable" level this round; skipped per the round prompt's optional clause.

---

## Expected em-dash rates on the Peer Median column

After CSV regen, the Peer Median column rendered for a typical screener-universe ticker will have em-dashes at roughly the following per-metric rates, driven by data availability and the min_peers=5 threshold within each industry_group bucket:

- **SVR — ~43% em-dash.** Several industry groups have <5 members with non-null SVR. SVR requires both market cap and revenue, and the smaller sector-thin buckets (Utilities sub-segments, niche Materials groups) cluster under the threshold.
- **Gross Margin (TTM) — ~27% em-dash.** Service-dominant tickers with the CostOfServices XBRL gap (documented in `DEVELOPMENT.md §11`) lack `gross_margin_ttm`, which thins out a few buckets below min_peers.
- **Operating Margin (TTM) and ROIC (TTM) — ~8% em-dash each.** Mostly the same long tail of small / data-incomplete buckets.
- **Revenue YoY, Revenue 3Y CAGR, FCF Margin (TTM), Rule of 40 — minimal (<5%) em-dash.** These metrics are computed from the most universally-available XBRL primaries.

ETFs and classifier-Unknown tickers will see all eight metrics em-dashed plus the inline "not applicable for ETFs" note (ETFs only — Unknown classifier still gets the grid, just empty).

These rates are estimated from the metric-availability statistics observed in `diagnostics/diag_rubric_audit.py` against the current universe; owner verification will produce the actual numbers post-CSV-regen.

---

## Owner verification steps (required before merge to `main`)

1. **Regenerate `screener_results.csv`** — `python fundamental_screener.py --universe universe_prescreened.csv --csv-out screener_results.csv`. New columns appear: 8 `peer_median_*` + `peer_count`. Dropped: `svr_vs_sector_median`.
2. **Diff `leaders.csv` before and after rebuild** — `python leader_selector.py --build`. The `+5` score bonus from `svr_vs_sector_median <= 1.0` is gone, so a handful of tickers that were borderline LEADER/GEM may flip down to WATCH and vice versa. Quantify churn against the pre-Round-7d `leaders.csv` to confirm the drift is within the expected ±5 score window.
3. **Restart the API server** so the `verdict_provider` mtime cache picks up the new CSV, and so `as_of_csv_mtime` is computed against the freshly written file.
4. **Hard-refresh `localhost:8000`** so the frontend picks up the JS changes.
5. **UI spot-check 5 ticker types on Ticker Lookup:**
   - **Profitable non-override (NVDA, AAPL, MSFT)** — verdict card grid shows three columns. Company values populated; Peer Median column populated for the 8 peer-able metrics (em-dash for the categorical rows like Sector / Industry Group / Industry). Timestamp chip visible above the SCORE box; hover reveals the raw ISO mtime.
   - **Profitable override (GOOGL, META)** — same as above. Override status doesn't affect peer comparison (override is symbol→tier mapping; bucket membership is by industry_group either way).
   - **Loss-making (any ticker with negative trailing earnings)** — Peer Median populated where the metric exists (Operating Margin can still be positive even when EPS is negative); Rule of 40 may be em-dash if its underlying metrics are nulled.
   - **Small-bucket industry_group (any ticker classified into a bucket with <5 members for the metric)** — Peer Median em-dash on the affected metrics; verdict card otherwise renders normally.
   - **ETF (SPY)** — Peer Median column all em-dash; inline note "Peer median comparison not applicable for ETFs." renders below the grid; P/E card on the Compare card shows mouse-hover tooltip "Weighted average of holdings."
6. **Spot-check Daily Report and Leader Detector** — neither tab consumes peer-median fields directly, but verdict cards opened inline (Round 7a) flow through the same `buildVerdictCard` and should show the same three-column grid + timestamp chip.

---

## Files touched (Phases 2–4 + this docs commit)

- `fundamental_screener.py` (+60 lines: `PEER_MEDIAN_METRICS` const, `apply_peer_medians` function, `run_full_screen` hook, 9 columns added / 1 removed in `CSV_OUT_FIELDS`, `score_ticker` bonus removal)
- `verdict_provider.py` (+11 lines: 8 `peer_median_*` in `_FLOAT_COLS`, `peer_count` in `_INT_COLS`, `as_of_csv_mtime` injection in `load_verdict_for_symbol`)
- `tests/unit/test_peer_median.py` (new, 6 tests)
- `tests/unit/run_all.py` (+1 line + 1 import)
- `frontend/index.html` (verdict card grid refactor + timestamp chip + ETF tooltip + inline note + ETF-set cache reuse; net +90 / -25 across the three frontend commits)
- `round7d-summary.md` (this file, new)
- `DEVELOPMENT.md` (+8 lines: schema note + caching note)
- `CHANGELOG.md` (Round 7d entry)
- `FEATURE_BACKLOG.md` (FB-1 display half + FB-4 marked shipped)
