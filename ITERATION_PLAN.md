# Quantfolio — Next Iteration Plan

**Iteration scope:** Priority 1 from `NEXT_ROUNDS.md` — the correctness-and-reliability block. One timing mini-fix plus three numbered rounds (4, 5, 6) covering the remaining CRITICAL and one HIGH finding from `audit-findings.md`.

**Starting point:** `main` at `11fe90f`, post-doc-sync. Code pointers in this plan were re-checked against HEAD on 2026-04-23.

**Out of scope this iteration** (carried forward to the one after this):
- Round 7 UX batched cleanup (Priority 2 — H-6..H-17 + N-1..N-14)
- ML methodology review (Priority 4)
- Security audit (Priority 4)
- New features from daily usage (Priority 4)

---

## Why this shape

`NEXT_ROUNDS.md` recommends five moves in order: timing fix → Round 4 → Rounds 5/6 back-to-back → pause and use → Round 7. This plan covers steps 1–3. Rationale for stopping there:

- Everything in this iteration improves **what the numbers mean or how fresh they are** — correctness territory the user has to be able to trust before doing another UX pass.
- Round 7 is a batched cleanup that benefits from a week of real-world usage feedback between iterations (per `NEXT_ROUNDS.md` step 4). Shipping Rounds 4–6 first and then sitting with the tool for a few days will produce a better-targeted Round 7 scope than bundling everything now.
- All three rounds here are individually small (30 min–2 h). Keeping them in one iteration lets us ship a single "correctness" CHANGELOG entry rather than three tiny ones.

**Total estimated engineering time:** 2.5–4 hours across four discrete commits. Plus ~30 min of manual verification (one real Daily Report run, one Ticker Lookup smoke test, one simulated-rate-limit test).

---

## Mini-round 0 — Daily Report timing measurement

**Type:** mini-fix, no agent.

**Why it goes first:** It is a 3-line code change blocked only on one real measurement. Doing it before Round 4 means the timing banner is honest in the screenshots and CHANGELOG entries we produce for the rest of the iteration. It is also the cheapest thing on the board and clears `NEXT_ROUNDS.md` step 1.

**Measurement protocol:**
1. Clear `data_cache/dual_report_*.json` so the path is cold.
2. Click **Refresh Report** with phone stopwatch running.
3. Record (a) wall-clock elapsed, (b) machine spec in one sentence (laptop vs desktop, SSD vs HDD, CPU).
4. Round up to the nearest 5-minute mark for the user-facing number.

**Three touch points** (already flagged in `round2-summary.md` line 152 and `NEXT_ROUNDS.md` Priority 3):

| File | Location | Current value |
|---|---|---|
| `frontend/index.html` | `:488` (`DAILY_REPORT_EST`) | `'25–55 min'` |
| `api_server.py` | `:589` (scan-started banner) | `"This may take 25-55 minutes."` |
| `USER_GUIDE.md` | Parts 4 and 11 | `25–55 min` band |

**Commit message:** `fix: update Daily Report timing banner to measured value (Mini-round 0)`.

**Expected diff size:** 3 lines + a one-line commit body noting the measurement hardware.

**Test gate:** eyeball the three touch points post-edit for consistency. No regression test needed — it is a copy change.

---

## Round 4 — Reliable data fetching (C-4 + C-5)

**Rationale for highest priority among the three rounds:** on a flaky Yahoo or SEC day, tickers currently drop silently from scans. The user sees "no data available" for a ticker that is actually healthy but temporarily rate-limited. This is the finding with the broadest downstream blast radius: it affects the Daily Report, the Leader Detector rebuild, and first-time Ticker Lookup — i.e. all three user-facing data paths.

### Scope

A single new module `http_client.py` that both `edgar_fetcher.py` and `finance_model_v2.py` can use, replacing the two drifted one-shot fetch paths.

**Module surface:**

```python
# http_client.py
def get_json(url: str, *, headers: dict, timeout: float = 30.0,
             max_retries: int = 5, rate_limiter: TokenBucket | None = None,
             retry_on_empty: bool = False) -> dict: ...

def retrying_df_fetch(fetch_fn, *, max_retries: int = 3,
                      rate_limiter: TokenBucket | None = None) -> pd.DataFrame: ...
    # Wraps yf.download; treats empty DataFrame as retryable.

class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int): ...
    def acquire(self) -> None: ...  # Blocks until a token is available.
```

**Retry contract** (shared by both entry points):
- Exponential backoff: base delay 1s, multiplier 2, max 5 attempts, jitter 0–1s (matches the existing `_download_batch` shape at `finance_model_v2.py:240-247`).
- On HTTP 429: honor `Retry-After` header if present; else fall through to exponential backoff.
- On HTTP 5xx: retry.
- On HTTP 4xx (except 429): raise immediately — permanent failure, no point retrying.
- On empty DataFrame (`retry_on_empty=True`): same backoff, treated as 429-equivalent. yfinance's 429 surfaces this way, not as an exception.
- After `max_retries` exhausted: raise a typed `HttpRetryExhausted` so callers can surface a user-visible warning count, not a silent drop.

**Rate limiter:**
- One module-level `SEC_LIMITER = TokenBucket(rate_per_sec=10, capacity=10)` used by every SEC call (both `companyfacts` and `submissions` endpoints). This collapses the facts+submissions burst that `audit-findings.md` C-4 flagged (`edgar_fetcher.py:366-387`).
- No shared limiter for yfinance — Yahoo does not publish a rate, and the existing `FETCH_DELAY_SEC` per-batch sleep continues to apply.

### Replacement sites

**`edgar_fetcher.py`:**
- `http_get_json` at `:180-186` — replace `urllib.request.urlopen` body with `http_client.get_json(url, headers=..., rate_limiter=SEC_LIMITER)`. Preserve the function signature so `load_ticker_cik_map`, `_fetch_submission_meta`, and `fetch_one` do not change.
- `fetch_one`'s try/except at `:274-287` — today a single HTTP 429 writes status `error` and moves on. After Round 4, the retry happens inside `get_json` so the outer try/except only sees genuine failures (404, network gone). No API change there.
- Delete `time.sleep(RATE_LIMIT_SLEEP)` at `:368` and `:389` — the token bucket replaces them. This also structurally closes the facts+submissions burst bug.

**`finance_model_v2.py`:**
- `_download_batch` at `:239-247` — replace the bare `yf.download` with `retrying_df_fetch(lambda: yf.download(...), max_retries=MAX_RETRIES)`. Keep the outer retry loop for now — yfinance can also raise, and those cases should still hit the existing exponential-backoff-with-jitter path.
- `fetch_stock_data` at `:249-267` — on empty DataFrame after retries, emit a `[scan] {sym}: rate-limited after {N} retries, skipping` log line at warn level, not the current silent `continue` at `:264`.

**`api_server.py`:**
- Scan orchestrator that aggregates skipped tickers — add a `rate_limited_skips: int` field to the scan summary JSON. Daily Report can surface it as a subtle "N tickers temporarily unavailable" footer chip. This is the user-visible half of C-5.

### Phase breakdown (one commit per phase)

| Phase | What lands | Why separate |
|---|---|---|
| **4.0** | `http_client.py` module + 6 unit tests in `tests/unit/test_http_client.py` (token bucket math, backoff schedule, `Retry-After` parsing, 4xx no-retry, 5xx retry, empty-DF retry) | Extract the shared primitive before any caller touches it. Lets wright review the API surface in isolation. |
| **4.1** | Route `edgar_fetcher.http_get_json` through `http_client.get_json`; drop the per-ticker `time.sleep(RATE_LIMIT_SLEEP)` | SEC path on its own. Easy to revert if the token bucket misbehaves under load. |
| **4.2** | Route yfinance through `retrying_df_fetch`; surface `rate_limited_skips` in the scan summary | Separate commit so that if yfinance retries mask a real problem we can narrow the blame without reverting the SEC work. |
| **4.3** | Doc sync — `DEVELOPMENT.md §7` (rate-limit discipline claim), `CHANGELOG.md` (Round 4 entry), `TWO_LAYER_ARCHITECTURE_PLAN.md` if it still references the old path. | Docs match code. Mirrors the shape the Round 3 work used. |

### Test gate

All four of these must pass before merge:

1. **Unit tests** (new): 6 in `test_http_client.py` above. Mock HTTP 429 with `Retry-After: 2`, assert the retry happens after 2s and the client succeeds. Mock three successive empty DataFrames, assert the client retries and finally raises `HttpRetryExhausted`.
2. **Integration**: `python edgar_fetcher.py --universe Tickers.csv --force` on a 10-ticker subset, with the network briefly pinched (`tc qdisc add dev` on Linux, or a stub HTTP server on Windows). No tickers should be marked `error` that would have succeeded on an unpinched run.
3. **Regression**: existing 24 unit tests in `tests/unit/run_all.py` continue to pass unchanged.
4. **Live smoke**: one Daily Report end-to-end run — no silent drops, and the `rate_limited_skips` field is present in the summary (even if zero).

### Suggested team

Three-agent team, lifted straight from Rounds 1–3:

- **skipper writes** Phases 4.0–4.2. The primitive and the two caller rewires are classic skipper work — narrow, numerical, well-defined.
- **wright reviews design** at the Phase 4.0 boundary (API surface of `http_client.py`) and again at the end of Phase 4.2 (has the rate limiter centralized the facts+submissions burst?). Wright has the best track record on "this is the right abstraction" calls.
- **sophia reviews user-visible surfaces** after Phase 4.2. Specifically: does the `rate_limited_skips` chip render correctly when non-zero? Is the log line at the right level (info vs warn)?

**Estimated effort:** 1.5–2 hours writing, plus 30 min review loop. Matches `NEXT_ROUNDS.md`'s 1–2 h estimate.

### Risk and mitigation

- **Risk:** a bug in `TokenBucket` throttles SEC calls too aggressively and turns a ~3.5 h cold rebuild into a ~5 h rebuild. **Mitigation:** the Phase 4.0 unit tests include a rate-math check (`acquire()` 20 times, assert total wall clock ≥ 2.0s at 10 req/s). And Phase 4.1 is a single revertable commit.
- **Risk:** yfinance retries hide a real upstream break. **Mitigation:** `HttpRetryExhausted` is raised, not swallowed. Callers log at warn level with retry count.
- **Risk:** changing `http_get_json`'s error-path signature breaks `fetch_one`'s existing HTTP 404 branch at `edgar_fetcher.py:277`. **Mitigation:** keep 4xx passthrough — retries apply only to 429/5xx. The 404 branch must still see `urllib.error.HTTPError` exactly as today. The unit tests pin this.

### Commit topology target

```
<pending>  docs: Round 4 — retry + rate limiting; update DEVELOPMENT §7 and CHANGELOG
<pending>  refactor: route yfinance through retrying_df_fetch; surface rate_limited_skips (Phase 4.2)
<pending>  refactor: route edgar_fetcher.http_get_json through shared client (Phase 4.1)
<pending>  feat: shared http_client module with retry, Retry-After, and token bucket (Phase 4.0)
```

---

## Round 5 — Stale-feature warning (C-9)

**Rationale:** `predict_ticker`'s silent fallback at `finance_model_v2.py:507` (`if np.isnan(lf).any(): lf=aX[-1:].copy()`) is the highest-impact correctness issue left because it affects what the user *sees* in the most frequently-used tab. A confident green BUY card built from yesterday's features is strictly worse than a visible "data incomplete today" notice.

### Scope

Three surgical changes plus one doc line.

**1. `finance_model_v2.py:505-508`** — detect the fallback and thread it through the return dict:

```python
# Predict next day
lf=latest_row[fcols].values
warnings=[]
if np.isnan(lf).any():
    lf=aX[-1:].copy()
    warnings.append("stale_features_used")
ls=scaler.transform(lf.reshape(1,-1))
...
result={..., "warnings": warnings, ...}  # always present (empty list if clean)
```

**Why `warnings` as a list, not a scalar flag:** forward-compat. Round 3's `verify_phase*` baselines and the Daily Report code already handle "extra fields present, old callers ignore them" cleanly. Future warning strings (e.g. `"limited_history"`, `"svr_unavailable"`) slot in without another schema bump.

**2. `predict_ticker_compare` at `:603-619`** — copy the two `warnings` arrays onto the compare-card response under keys `v2_warnings` / `v3_warnings`. This is three lines.

**3. `frontend/index.html`** — Ticker Lookup render path. When either `v2.warnings` or `v3.warnings` includes `stale_features_used`, show a muted amber inline notice directly above the signal pill:

```html
<div class="tl-stale-notice" role="status" aria-live="polite">
  Some features are missing for today; prediction uses yesterday's values.
</div>
```

Re-use the existing N/A pill's dashed-grey styling family (established in Round 2 Bucket 2) — keep it visually quieter than the verdict-card banners so users do not conflate it with BUY/SELL/HOLD semantics.

**4. `README.md` API Endpoints block** — add a one-liner: "Prediction responses include a `warnings` array; `stale_features_used` indicates today's feature row had NaN values and yesterday's features were used as fallback."

### Phase breakdown

Single commit. This is a localized change touching four files with small diffs.

### Test gate

1. **Unit-adjacent**: construct a df where `df.iloc[-1:][fcols].values` has an NaN, call `predict_ticker`, assert `"stale_features_used" in result["warnings"]`. (Plain-assert style, goes into `tests/unit/test_predict_ticker_warnings.py`.)
2. **Negative case**: construct a clean df, assert `result["warnings"] == []`.
3. **Manual**: spot-check one ticker on Ticker Lookup — ideally a recent IPO where the ROC_60d feature is still warming up — and confirm the inline notice renders. If no IPO qualifies on the day of testing, temporarily monkey-patch `latest_row` in a dev console to inject NaN and verify the UI path.

### Suggested team

`NEXT_ROUNDS.md` Round 5 brief says "skipper + sophia (no wright needed — small, localized)." Agree. Single back-end + single front-end change, no architectural question.

**Estimated effort:** 30 min writing, 15 min review.

### Risk and mitigation

- **Risk:** the inline amber notice is too loud and distracts from the main signal. **Mitigation:** sophia review gate — use the existing dashed-grey pill family, not the SELL-red or WATCH-amber families already in play.
- **Risk:** "warnings": [] becomes a Pandas-style catch-all that grows to dozens of strings. **Mitigation:** keep the enum tight — document the three strings we plan to add in this iteration (`stale_features_used` now, `limited_history` and `svr_unavailable` reserved for Round 7). Anything beyond those needs a doc update, not a silent add.

---

## Round 6 — Pro model availability banner (H-3)

**Rationale:** Lite-vs-Pro comparison is Quantfolio's headline differentiator per the README. When LightGBM is absent, the Pro column silently says "Not available" with no instruction. This is the one HIGH-priority finding that directly undermines a first-run product story, and it can ship in the same iteration as Round 5 with no coupling.

### Scope

Four small changes.

**1. `api_server.py`** — add a new endpoint `GET /api/system/status` that returns:

```json
{
  "has_lgbm": false,
  "model_version": "v2",
  "pro_available": false,
  "notes": {
    "pro_unavailable_reason": "lightgbm package not installed",
    "install_hint": "pip install lightgbm"
  }
}
```

`has_lgbm` is already imported at `api_server.py:56` and available as a module-level boolean. The endpoint is 10 lines.

**2. `frontend/index.html`** — on page load (alongside the existing `/api/symbols` fetch that powers the footer count), fire `/api/system/status`. If `pro_available === false`, render a persistent dismissible banner above the tab strip:

```html
<div class="app-banner banner-info" role="status">
  Pro model unavailable — install <code>lightgbm</code> to enable Lite-vs-Pro comparison.
  <button aria-label="Dismiss">×</button>
</div>
```

Dismiss state lives in `sessionStorage` (not `localStorage` — we want it to reappear on next browser session, per `NEXT_ROUNDS.md`'s "dismissible but reappears on next server restart" hint, and sessionStorage is the closer fit).

**3. `README.md` — API contract note.** Add to the API Endpoints block:

> **Model availability:** prediction responses include `v2` (Lite) and `v3` (Pro) fields. `v3` may be `null` when `lightgbm` is not installed — callers should treat `null` as "Pro unavailable, fall back to Lite" rather than an error.

(This text is already in the README per line 300 — check it survived the doc sync. If it did, this step is a no-op. If it didn't, restore it.)

**4. Remove dead `#modelToggle` code** — `frontend/index.html:896` and `:902`. This is H-8 from the audit, folded in because it's 3 lines away from the banner code and leaving it is silly once we're in this file.

### Phase breakdown

Single commit, four files.

### Test gate

1. **Manual with LightGBM present**: page loads, no banner. Predict one ticker, Pro column populated.
2. **Manual with LightGBM absent**: `pip uninstall lightgbm` in a throwaway venv, start the server, load the page. Banner appears. Dismiss it — stays dismissed on SPA navigation (tab switch). Reload the page — banner reappears. (Test both `sessionStorage` behavior edges.)
3. **API**: `curl http://localhost:8000/api/system/status` returns the expected JSON shape in both states.

### Suggested team

`NEXT_ROUNDS.md` Round 6 brief says "single agent, sophia preferred." Agree — this is UX copy plus a trivial backend endpoint.

**Estimated effort:** 30 min.

### Risk and mitigation

- **Risk:** the banner is implemented as a toast-style overlay that covers controls on smaller windows. **Mitigation:** render it in normal document flow above the tab strip, not as a fixed overlay. The Round 2 skeleton banners ship this pattern correctly — reuse their CSS class family.
- **Risk:** `/api/system/status` becomes a dumping ground for every future runtime flag. **Mitigation:** keep the response shape minimal in this iteration. One new field every 6 months is fine; the moment it approaches 10 fields, refactor to per-capability endpoints.

---

## Sequencing and merge strategy

**Recommended order of commits on `main`:**

1. **Mini-round 0** (timing fix). Direct to `main`, no branch needed — 3-line change.
2. **Round 4 on a branch** `agent-round4`. Merge to `main` after all four phases + test gate pass.
3. **Round 5 + Round 6 on a combined branch** `agent-round5-6`. Two commits, one merge. Both are small enough that a single branch is lighter than two.

**Why not one mega-branch for Rounds 4–6:** Round 4 is the only one with non-trivial rollback risk (shared HTTP client). Keeping it separate means if the token bucket misbehaves in production, a revert of one branch drops it cleanly without losing Rounds 5 and 6. This matches Round 2's Bucket 2 strategy of keeping high-risk changes mergeable in isolation.

**CHANGELOG strategy:**
- One user-visible Round 4 entry: "Data fetches now retry on rate limits instead of silently dropping tickers."
- One combined Round 5+6 entry: "Ticker Lookup now flags stale-feature predictions, and the dashboard surfaces Pro-model availability on first load."

---

## What this iteration does NOT ship

Keeping these explicitly out so the next planning round does not re-ask:

| Deferred item | Reason | Next-up iteration |
|---|---|---|
| Round 7 batched UX cleanup (H-6..H-17 + N-1..N-14) | Benefits from a week of real usage feedback before scoping | Iteration after this one |
| H-2 (`/api/report` no age check on disk load) | Wright flagged as borderline CRITICAL in Round 2 follow-ups. Worth a dedicated Round 4.5 if it's the first thing the user hits after Round 4 merges. | Iteration after this, or inline if user reports it |
| H-4 (`/api/screener/refresh` orphan endpoint) | Decision: wire a UI button or delete the endpoint. Needs a product call, not an engineering call. | Discuss with user before next iteration |
| H-5 (`**kwargs` silent drop on predict) | Architectural — argues for a Pydantic request model. Round 7 candidate, not urgent. | Iteration after this |
| ML methodology review (survivorship, look-ahead, out-of-sample regimes) | Multi-session research-assistant work; `NEXT_ROUNDS.md` Priority 4 | Separate iteration with explicit scope |
| Security audit | Dedicated read-only round similar to Round 1 | Separate iteration |

---

## Verification plan (iteration-wide)

Before declaring the iteration done:

- [ ] All 24 existing unit tests + new Round 4 unit tests pass — `python tests/unit/run_all.py`.
- [ ] `verify_phase3.py`, `verify_phase4a.py`, `verify_phase4b.py` still pass on 7/7 baseline tickers. (Round 4 changes data fetching, not backtest math, but running them confirms nothing regressed.)
- [ ] One cold Daily Report run end-to-end with the new timing banner. Record wall-clock time and update Mini-round 0's value if it drifts from the measurement taken at the start.
- [ ] One Ticker Lookup click on five tickers — at least one recent IPO (for C-9 path), at least one where SVR is unavailable, at least one ETF (SPY or QQQ), at least one LEADER, and one INSUFFICIENT_DATA (CRWD). Spot-check the verdict card, the inline notice (if any), and the Pro-availability banner all render as expected.
- [ ] `rate_limited_skips` field is present (value 0 is fine) in `/api/report` response.
- [ ] `/api/system/status` returns the new shape.
- [ ] CHANGELOG has one entry for Round 4 and one combined for Round 5+6. No "Round 4.5" without explicit approval.

---

## Notes on agent workflow (carried from Rounds 1–3)

- **Three-agent team for Round 4 only.** Rounds 5 and 6 run with single or double-agent teams per the `NEXT_ROUNDS.md` guidance that "three-agent teams are overkill for small, focused work."
- **Test-first for Round 4.** Phase 4.0 writes the unit tests before the callers are rewired — same shape that made Round 3 safe.
- **Manual UI verification is non-negotiable for Rounds 5 and 6.** Round 2 shipped two regressions that would have been caught in 5 minutes of clicking. Do not skip the manual gates.
- **Atomic commits per phase.** Round 4 is four commits, Round 5 is one, Round 6 is one. Total of six commits on `main` after this iteration, plus the Mini-round 0 timing fix.
- **Accept-edits mode** as default per Round 3's finding.

---

## Reference — where things live (updated for this iteration)

- `NEXT_ROUNDS.md` — parent memo this plan derives from
- `audit-findings.md` — Round 1 source for C-4, C-5, C-9, H-3
- `round3-summary.md` — prior-round template for phase breakdown and verify scripts
- `backtest_engine.py` — the shared primitive pattern Round 4 mirrors
- `verdict_provider.py` — another shared-primitive example; new `http_client.py` follows this shape
- `tests/unit/` — target for Round 4's 6 new unit tests

---

Written 2026-04-23, post-doc-sync. Ready to pick up when the team is.
