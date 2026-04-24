# Round 5 + 6 — Summary

Branch: `agent-round5-6` (three commits, not yet merged to `main`).

## What shipped

| Commit | Title | Finding |
|---|---|---|
| `cdc861d` | fix: surface stale-features warning in Ticker Lookup (C-9) | C-9 |
| `66a504b` | fix: add Pro model availability banner and /api/system/status endpoint (H-3) | H-3 |
| `a31d5b4` | refactor: remove dead single-model-toggle code path (H-8) | H-8 |

### Round 5 — C-9 stale-features warning (`cdc861d`)

- `finance_model_v2.py::predict_ticker` now emits `warnings=["stale_features_used"]` whenever today's feature row has NaN and the prediction falls back to yesterday's complete row. Always-present key (empty list on clean data) so future warning strings slot in without a schema bump.
- `predict_ticker_compare` copies each side onto the compare-card response as `v2_warnings` / `v3_warnings`.
- `frontend/index.html` compare card renders a muted dashed-amber inline notice directly above the consensus pill when either model is stale. Reuses the Bucket 2 N/A-pill family — visually quieter than the verdict banners so it is not confused with BUY/SELL/HOLD.
- `README.md` API block documents the `warnings` array.
- New `tests/unit/test_predict_ticker_warnings.py` (2 tests: NaN path + clean path) registered in `run_all.py`.

### Round 6 — H-3 Pro model availability banner (`66a504b`)

- New `GET /api/system/status` returns `has_lgbm`, `pro_available`, and human-readable notes with an install hint.
- `frontend/index.html` now fires `/api/system/status` on page load alongside `/api/symbols`. If `pro_available === false` and the user has not dismissed the banner this browser session, a dismissible banner is shown above the tab strip.
- Banner uses a new `.app-banner` CSS class reusing the scan-banner/skeleton-empty visual family. Renders in normal document flow — never a fixed overlay.
- Dismiss state stored in `sessionStorage` under `quantfolio_pro_banner_dismissed` (per NEXT_ROUNDS guidance: stays dismissed this session, reappears on next session or server restart).
- README already covers the null-`v3` contract (line 300) and the LightGBM requirement (line 75); no README change was needed.

### H-8 dead-code removal (`a31d5b4`)

Split from the H-3 banner commit because the deletion was ~130 lines. Removed:

- `let selectedModel = 'v3'` (module-level, read only by `runPredict`)
- `setModel(ver)` and its `#modelToggle .mtog` query (DOM node never rendered)
- `async function runPredict()` (unreachable — no caller anywhere)
- `function buildSingleCard(d)` (called only by `runPredict`)

`#modelHint` is kept; its static placeholder text already reflects the compare-only flow.

## Verification status

### Automated (done)

- `python tests/unit/run_all.py` — **41 / 41 passing, 0 failures** after the Round 5 commit and again after the Round 6 + H-8 commits. Output verified locally against the miniconda Python interpreter.
- `ast.parse` syntax check on `api_server.py` passes after the new endpoint is added.

### Manual click-through (deferred to user)

Per the round plan, manual UI verification is the user's step. Specifically:

- **C-9 stale-feature notice** — pick one recent IPO (ROC_60d warming up), or monkey-patch `latest_row` in a dev console to inject NaN, and confirm the dashed-amber notice renders directly above the consensus pill in the Ticker Lookup compare card. The notice must NOT appear on clean-data lookups.
- **H-3 banner with LightGBM present** — page loads, no banner. Predict one ticker, Pro column populated normally.
- **H-3 banner with LightGBM absent** — in a throwaway venv, `pip uninstall lightgbm`, start the server, load the page. Banner should appear above the tab strip. Dismiss it — stays dismissed while clicking between tabs. Reload the page — banner reappears (sessionStorage is per-session, but reload is still the same session; it will only clear when the tab is closed). Close the tab and open a new one — banner reappears.
- **H-3 endpoint shape** — `curl http://localhost:8000/api/system/status` returns the new JSON in both LightGBM-present and LightGBM-absent states.
- **H-8 regression check** — Ticker Lookup Predict button still works and shows the compare card; no console errors about missing `setModel` / `runPredict` / `buildSingleCard` / `#modelToggle` / `selectedModel`.

## Follow-up items noticed

- **C-9 frontend UX nit:** the inline stale-feature notice lands on the compare card only. The single-predict card would also have benefited, but that render path (`buildSingleCard`) was the dead code removed in `a31d5b4`. If the single-predict endpoint is ever reintroduced (or called directly via URL / external tooling), any future rendering of the single-predict response should read `d.warnings` and surface the same notice.
- **H-3 endpoint surface:** per the plan's "dumping ground" risk note, keep `/api/system/status` minimal. The current 4-key payload is the budget; any new capability flag beyond ~10 total fields should trigger a refactor to per-capability endpoints.
- **C-9 warnings-enum discipline:** `stale_features_used` is the only string we emit today. `limited_history` and `svr_unavailable` were flagged in the plan for Round 7. Any fourth string should land with a docs update to keep the enum tight.
- **Test-suite runtime:** the full unit suite (now 41 tests) takes ~18–20 minutes locally because `test_api_backtest_wire_format` runs three multi-strategy backtests on ~2800 days of KO data. Not in scope for this round, but worth a future look if CI time becomes a concern.

## Session end

Two pieces of work landed, three commits on `agent-round5-6`. Branch is ready for user review and merge to `main`.
