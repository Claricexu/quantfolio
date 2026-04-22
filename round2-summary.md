# Quantfolio round 2 — implementation summary

Branch: `agent-round2` (7 new commits ahead of `main`).
Not pushed, not merged. Review / merge decision is yours.

Dispatched as a three-agent team (skipper writes, wright + sophia review) against `audit-findings.md`. Buckets 1, 2, 4 addressed; Bucket 3 (backtest refactor) deliberately deferred.

---

## Findings completed

| ID | Title | Commit(s) |
|----|-------|-----------|
| **Bucket 1 — one commit per finding** | | |
| C-7 | Pin requirements.txt to exact versions | `4df8e2f` |
| C-6 | Enable SQLite WAL + busy_timeout on fundamentals.db | `941bd40` |
| C-8 | Separate long-poll fetches from timeout enforcement | `d23beac` + `b36734d` (pluralize follow-up) |
| N-11 | Correct ticker count in footer | `e5fbac5` |
| H-15 | Make library rows discoverable as clickable | `443aa54` |
| **Bucket 2 — one commit, four findings** | | |
| C-1, H-1, H-10, N-13 | Unify fundamental verdict reads across tabs with reason codes | `78d11d3` |
| **Bucket 4 — one commit + follow-up, six findings** | | |
| C-2, C-10, C-11, C-12, H-11, H-12 | UX safety rails (filter parity, honest timing, dep check, skeletons, destructive-action modal) | `44b75a6` + `4aa8798` (info icon render follow-up) |

Total: **13 audit findings closed across 9 commits.**

---

## Findings skipped — and why

| ID | Title | Reason |
|----|-------|--------|
| C-3 | Three divergent backtest implementations | **Bucket 3 — deliberately deferred** per scope brief. Dedicated session needed. |
| C-4 | SEC EDGAR no retry / Retry-After | Wright flagged it collapses with C-5 into one HTTP-client refactor. Out of buckets 1/2/4. |
| C-5 | yfinance 429 silent drop | Same root as C-4. Deferred to a "shared HTTP client" session. |
| C-9 | `predict_ticker` stale feature fallback | Touches `finance_model_v2.py` — Bucket 3 scope. |
| H-2 | `/api/report` no age check on disk load | Out of buckets. Borderline CRITICAL per Wright — flag for next round. |
| H-3 | Pro model null with no UI indication | Out of buckets. |
| H-4 | `/api/screener/refresh` orphan endpoint | Out of buckets. `POST /api/screener/refresh` preserved through Bucket 2 since it's the SEC-pull path, orthogonal to the three-tab gaslight. |
| H-5 | `**kwargs` silently dropped | Out of buckets. |
| H-6 | `_load_library_summary` no caching | Out of buckets. |
| H-7 | No accessibility anywhere — color-only state | Partially addressed as side effects of H-11/H-12 (skeletons got `role="status"` + `aria-live`, modal got `aria-modal`/`aria-labelledby`/`aria-describedby`, N/A pill got dashed border as non-color affordance, ⓘ icon got `tabindex="0"`/`aria-label`). Full sweep still pending. |
| H-8 | Dead `#modelToggle` in README | Out of buckets. |
| H-9 | Ticker Lookup verdict fetch silent fail | Out of buckets. |
| H-13 | Strategy Lab no cancel / no ETA | Out of buckets. |
| H-14 | `start_dashboard.bat` miniconda assumption + early browser open | Out of buckets. Partially mitigated by C-10's fuller dep probe. |
| H-16 | Leader Detector chip counts incomplete | Out of buckets. |
| H-17 | `alert()` on empty leaders + Download CSV always enabled | Out of buckets. |
| N-1…N-10, N-12, N-14, N-15 | Nice-to-haves | Out of buckets. |
| All doc-vs-code mismatches (except USER_GUIDE Parts 4/11 via C-11) | | Out of buckets. Follow-up docs pass needed. |

---

## Files modified (across all commits)

- `requirements.txt` — pinned
- `requirements.lock` — **new**
- `edgar_fetcher.py` — WAL + busy_timeout
- `verdict_provider.py` — **new**, single-source-of-truth verdict loader
- `fundamental_screener.py` — `tests_json` / `dealbreakers_json` columns in CSV schema
- `api_server.py` — reason-enum endpoints, mtime-based `generated_at`, C-11 banner band
- `frontend/index.html` — the biggest surface; fetch split, footer count, library hint, verdict card reason rendering, N/A pill, "As of" chips, SELL→HOLD downgrade with ⓘ icon, Daily Report skeleton, Strategy Lab skeleton, Leader Detector skeleton with `<details>` CLI fallback, rebuild modal with focus trap
- `start_dashboard.bat` — full hard-dep probe + `--full-install` flag + lightgbm warning
- `USER_GUIDE.md` — Parts 4 and 11 timing bands synced to `25–55 min`
- `.gitignore` — earlier commit `b37c81c` covered `*.db-wal` / `*.db-shm` / `.claude/scheduled_tasks.lock` (addresses a C-6 follow-up Wright had logged)

Generated data refreshed (not committed — gitignored):
- `screener_results.csv` regenerated end-to-end with 1414 rows and the two new columns populated. Took ~2 minutes against the WAL-mode DB. Three yfinance "possibly delisted" warnings (HOLX, AL, SEE) — benign, pre-existing, not introduced by this round.

---

## Follow-ups discovered during work

These were **logged as non-blocking** during review and left for future rounds. Sorted by origin-bucket.

### From Bucket 1 reviews

- **C-7 / requirements.txt**
  - `python-dotenv` declared `>=1.0` (not installed in dev env). Install and re-freeze next pass.
  - Add CI check: `pip install -r requirements.txt` in a clean venv should produce a `pip freeze` matching `requirements.lock`.
  - Starlette 1.0.0 transitive — re-check upstream advisories once 1.x CVE cadence catches up (CVE-2025-62727 family).
  - Pandas 3.0.2: CVE-2024-9880 (`DataFrame.query` injection) exists upstream. Repo doesn't call `.query()` on user input — flag if that changes.
  - Consider `pip install --require-hashes` for true byte-level reproducibility.
  - `DEVELOPMENT.md §1` should note `requirements.lock` exists and how to refresh it.

- **C-6 / SQLite WAL**
  - Eight `sqlite3.connect(...)` sites in `diagnostics/diag_*.py` still lack `timeout=30.0` + `busy_timeout=30000`. WAL comes free (file-level persistent). Patch on next pass.
  - One-line comment at `edgar_fetcher.py:172` noting WAL is file-level persistent so diagnostics inherit it — prevents a future contributor from "helpfully" removing the PRAGMA.

- **C-8 / fetchTimeout**
  - Apply `fetchNoTimeout` to `pollLibraryChart` (~L1859) and `pollBatchStatus` (~L1921) — same failure mode.
  - `visibilitychange` hook to abort long-polls when the tab is hidden.
  - Extract the three near-identical "Still working" tickers into one helper.
  - `document.hidden` gating on the 1 Hz ticker to avoid burning CPU on background tabs.
  - `mm:ss` format once elapsed ≥ 60 s (users will routinely see 147 s+ during the first scan).
  - `role="status"` / `aria-live="polite"` on the three still-working status divs (belongs with H-7).
  - `loadReport` status copy: "Still working — 42s elapsed" reads truer than "last checked" for a single fetch. Optional.

- **N-11 / footer count**
  - Drop the `+` suffix if live count ever returns < the static 174 fallback.

- **H-15 / library rows**
  - `role="button"` + Enter/Space keyboard activation on rows (belongs with H-7).

### From Bucket 2 reviews

- **`/api/screener` list endpoint** does not populate `reason` / `reason_text` on INSUFFICIENT_DATA rows. The Leader Detector frontend mirrors `REASON_TEXT` to work around this (`frontend/index.html:546-562`). Future consumer of the list endpoint will silently get nothing. Centralise.
- **`REASON_TEXT` is duplicated** in backend (`verdict_provider._REASON_TEXT`) and frontend (`index.html:546-550`). Next time a reason is added, one will drift.
- **`get_csv_mtime_iso` is TZ-naive.** Works for a localhost dashboard; breaks if the API ever runs in UTC while the browser is in a different zone. Switch to `datetime.fromtimestamp(mtime).astimezone().isoformat()`.
- **Schema bump not versioned.** If a third JSON column lands, `REQUIRED_BUCKET2_COLUMNS` becomes a lie. Consider a sentinel header.
- **N/A pill color family** — dashed amber sits next to WATCH verdict amber and HOLD signal amber. Sophia recommends a neutral-grey dashed pill to reduce "pending / warning" confusion at-a-glance. Non-blocking but worth a one-line swap.
- **"As of HH:MM" chip** on Daily Report is attached to the Firm Score column header only. A user reading `Lite Chg` / `Pro Chg` has no freshness signal for those; consider a table-level caption chip showing both mtimes.
- **Dev `console.warn` gated to `localhost` / `127.0.0.1` only.** Won't fire from a LAN IP / staging host. Loosen.
- **Legacy-CSV degrade banner**: startup log warning fires if CSV lacks `tests_json`/`dealbreakers_json`, but a user seeing empty test dots in the verdict card won't read console logs. Surface as a subtle dashboard banner.
- **Doc drift**: `USER_GUIDE.md` Part 3 (Ticker Lookup) still says SVR hint reads "Overvalued" (UI says "Expensive") and advertises a unified "Predicted price (next day)" headline that the compare card doesn't have. Part 6 (Leader Detector) still calls the column "Selected" (header is `SEL`) and describes the Sector column as "SIC 2-digit industry code" when the UI shows broad buckets via `broadSector()`.

### From Bucket 4 reviews

- **C-2 ordering invariant**: `_bestStratMap` is assigned before `renderReport` on both initial load and poll, but a future refactor moving `loadScreenerMap` between them would leave `downgradeSells` running with `{}`. Add a one-line invariant comment at the two call sites.
- **`FULL_SIGNAL_STRATEGY_KEYS` hardcoded twice** — frontend Set (`index.html:495`) and backend tuple (`api_server.py:137`). Centralise before a third "full signal" key is added.
- **C-12 naive datetime**: see the B2 follow-up on TZ; same concern applies here.
- **H-12 focus trap array is hardcoded**. If a future "What does this do?" link appears in the modal, the trap silently misses it. Consider `modal.querySelectorAll('input,button,a[href]')` instead.
- **H-12 inline error uses `aria-live="polite"`**. For validation errors following user action, `aria-live="assertive"` is more defensible.
- **C-11 copy**: `25–55 min` is a conservative guess, **not a measurement**. The scan-measure attempt timed out at 10 min without a fresh report. A one-time real-hardware measurement should refine this. See "Verification status" below.
- **C-11 "enough time to make coffee"** — Sophia flagged as feature-team voice pressuring the user; soften to "can take anywhere from under 30 min to over an hour depending on your machine; safe to close the tab" once a real measurement lands.
- **Strategy Lab skeleton** — Sophia's nit: add "Click **Run All Backtests** above to start." Currently the skeleton describes what will happen but doesn't tell a first-time user which button to press.
- **H-12 inline error copy** — Sophia's nit: "Type **REBUILD** exactly (case doesn't matter)." is more informative than the current "Type REBUILD to confirm".
- **C-10 lightgbm warning** — for a solo owner who doesn't have "a developer" to ask, add an "or skip it; Lite predictions still work" exit ramp.
- **C-12 Refresh button** — clicking "Refresh Report" kicks off a 25–55 min job; the fallback copy should surface that cost inline.
- **C-2 ⓘ icon**: both `title` and `aria-label` carry the same string → some screen readers double-announce. Low priority.
- **`sklearn` vs `scikit-learn`**: the C-10 import probe uses `sklearn` (the import name), `requirements.txt` lists `scikit-learn` (the package name). Correct as-is; worth a comment for the next maintainer.

---

## Verification status

| Check | Result | Notes |
|-------|--------|-------|
| `python -c "import api_server"` | ✅ pass | Verified after every commit in Buckets 1, 2, and 4. |
| `verdict_provider.load_verdict_for_symbol('APA')` | ✅ pass | Returns TAXONOMY_GAP with Sophia's reason text. |
| `verdict_provider.load_verdict_for_symbol('XYZFAKE999')` | ✅ pass | Returns NO_SEC_FILINGS. |
| SQLite concurrent reader + writer (C-6 smoke) | ✅ pass | Writer holding 3 s IMMEDIATE lock; reader returned 3.3 M rows from `facts` without `SQLITE_BUSY`. |
| Grep for `window.confirm` inside `rebuildLeaders` | ✅ pass | Only surviving reference is the HTML comment explaining the replacement. |
| Grep for `40-90` / `40–90` in `api_server.py` + `frontend/index.html` | ✅ pass | All sites sync to `25–55 min` band. |
| USER_GUIDE.md Parts 4 and 11 cite the band | ✅ pass | Both lines updated. |
| Modal aria completeness | ✅ pass | `role="dialog"`, `aria-modal="true"`, `aria-labelledby`, `aria-describedby`, focus trap, Esc/overlay/Cancel close. |
| Screener regen populates `tests_json` / `dealbreakers_json` | ✅ pass | 1414 rows rewritten; ADBE spot-check shows real JSON. |
| Full Daily Report end-to-end scan | ❌ **not run** | A scan-measure attempt from an earlier session timed out the poller at 10 min without producing a new dual_report file. `25–55 min` band is a conservative guess, **not a measurement**. |
| Browser render of all four tabs | ❌ **not run** | The harness cannot launch a browser. Code-level checks only: imports clean, handlers intact, markup inspected. A real user should spot-check Daily Report, Strategy Lab, Leader Detector, and Ticker Lookup on the next launch. |
| `/api/report` live response | ⚠️ partial | Server imports clean; no live HTTP smoke completed because the measurement scan died. |

**Bottom line**: the app almost certainly starts (importable, no syntax errors, prior sessions have run it) and should serve all four tabs. But we did not complete a first-full-scan before closing the session. First action on next startup: click "Generate First Report" and measure. Update `DAILY_REPORT_EST`, `api_server.py:587`, and `USER_GUIDE.md` Parts 4/11 once the real number is in hand.

---

## Housekeeping notes

- Three non-finding commits landed on `agent-round2` during the session (not attributable to the team but worth logging): `b37c81c` (ignore `*.db-wal` / `*.db-shm` / `.claude/scheduled_tasks.lock` — addresses a C-6 follow-up Wright logged), `19b5944` (clean .gitignore duplicates), `a4be65d` (ignore local Claude permissions). Earlier `.gitignore` + agent-move commits handled the pre-session environment setup.
- `audit-findings.md` itself remains untracked on disk (intentionally — it was the review artifact that fed this round, not code). You may want to commit or remove it before handoff.
- Several one-off `diagnostics/*.log` / `*.err` / `*.ps1` scratch artifacts from the scan-measurement attempt are untracked. Safe to delete.

---

## Commit topology on `agent-round2`

```
4aa8798 fix: render visible info icon on downgraded SELL→HOLD rows (C-2 follow-up)
44b75a6 fix: UX safety rails - filter parity, honest timing, dep check, skeletons, destructive-action modal (C-2, C-10, C-11, C-12, H-11, H-12)
b37c81c Ignore SQLite WAL files and Claude runtime; commit audit findings to round2 branch
19b5944 Clean up .gitignore duplicates and overly broad db pattern
a4be65d Ignore local Claude permissions
78d11d3 fix: unify fundamental verdict reads across tabs with reason codes (C-1, H-1, H-10, N-13)
443aa54 fix: make library rows discoverable as clickable (H-15)
e5fbac5 fix: correct ticker count in footer (N-11)
b36734d fix: pluralize "still working" status line (C-8 follow-up)
d23beac fix: separate long-poll fetches from timeout enforcement (C-8)
941bd40 fix: enable WAL mode and busy_timeout on fundamentals.db (C-6)
4df8e2f fix: pin requirements.txt to exact versions for reproducibility (C-7)
```

Ready for merge review. Recommend running a fresh Daily Report end-to-end first to lock in the C-11 band before the merge.
