# Round 7c-2 — implementation summary

Branch: `agent-round7c-2`, four feature/fix commits ahead of `3638c86` (the last Round 7c docs commit) plus this docs commit. Not pushed, not merged. Single-feature round: swap the Sector card on Ticker Lookup for a P/E (Price-to-Earnings) card, plus two follow-up touchups landed after sophia's first review.

Two-agent team this round: **skipper** did the work across all four feature commits; **sophia** reviewed the UI after the initial swap and again after the two touchups.

The Sector card was redundant with the Verdict card below it (which already shows canonical Sector / Industry Group / Industry from `classifier.classify()` after Round 7c) and surfaced a known display inconsistency between the two surfaces — Yahoo's GICS Industry on the Sector card, the canonical Industry on the Verdict card. Replacing the Sector card with P/E eliminates the redundancy and resolves the inconsistency through deletion.

---

## What shipped

| Commit | Title |
|---|---|
| `b80ef45` | feat: pipeline adds pe_trailing column from yfinance to screener_results.csv and API responses |
| `1a40958` | feat: replace Sector Card with P/E Card on Ticker Lookup (no-FB) |
| `9ff08ad` | fix: P/E card font size 15px to peer Market Cap and Quarterly Revenue |
| `f619843` | fix: SVR-N/A renders at full card size with em-dash, matching populated layout |
| _(this commit)_ | docs: round7c-2 summary, schema notes, CHANGELOG |

51 tests still pass after each commit; no test impact expected and none observed (changes are pipeline plumbing + CSS/render only).

**CSV regeneration required post-merge.** Commit `b80ef45` adds a `pe_trailing` column to `screener_results.csv`'s `CSV_OUT_FIELDS`, but the on-disk CSV has not been re-run. Until the owner runs `python fundamental_screener.py --universe universe_prescreened.csv --csv-out screener_results.csv`, every row's `pe_trailing` will be missing and the P/E card will display "—" for every ticker. This is intentional per the round prompt — code lands first, CSV regen is the owner's verification step.

---

## Phase 1 — trace findings (no code)

Skipper read four data-path files plus the frontend before touching anything.

- **yfinance** — `info.get('trailingPE')` is the right field. yfinance returns `float` (or `None`) directly; can be negative (loss-making companies), very large (post-write-down artifacts), or absent.
- **`fundamental_metrics.py`** — `compute_metrics(symbol, ...)` already has an `info` dict in scope at the time the metrics are computed, but that dict comes from `edgar_fetcher.get_ticker_info` (SEC fields: `name`, `sic`, `sic_description`, `status`, `last_fetched`) — not yfinance. yfinance facts are pulled inside small per-fact helpers (`get_market_cap`, `get_dividend_yield`) that maintain module-level memoization caches. The natural pattern is therefore a sibling helper `_get_trailing_pe(symbol)` with the same cache pattern, not a one-liner on the SEC `info` dict.
- **`fundamental_screener.py`** — `CSV_OUT_FIELDS` is a single list at module scope. Trivial append after `svr_vs_sector_median` to keep the valuation cluster contiguous.
- **`api_server.py`** — `_inject_classifier_fields` reads the screener row from `verdict_provider.load_screener_index()`. `verdict_provider`'s `_FLOAT_COLS` whitelist drives type coercion; columns not in the whitelist arrive as raw CSV strings. `pe_trailing` is not in that whitelist, so it must be coerced to float in `_inject_classifier_fields` before it surfaces on the wire. (Adding `pe_trailing` to `_FLOAT_COLS` itself would have been an alternative, but that touches a module the round prompt didn't explicitly include in scope; the localized coercion in `api_server.py` is a smaller change.)
- **`frontend/index.html`** — Sector card render is single-purpose, lives inside `buildCompareCard(d)` (Ticker Lookup compare card), tied to a 4-cell grid styled as `.svr-grid` / `.svr-box`. No reusable component, no other call site. Safe to delete in place.

No surprises beyond the verdict_provider whitelist gap, which is documented inline in the api_server change.

---

## Phase 2 — backend pipeline (commit `b80ef45`)

**`fundamental_metrics.py`.** New `_get_trailing_pe(symbol)` helper modeled on `get_market_cap` / `get_dividend_yield`: module-level `_pe_cache` dict, single `yf.Ticker(symbol).info.get('trailingPE')` call per process per symbol, `None` returned on any exception. Both `compute_metrics()` and `_empty_metrics()` set `m['pe_trailing'] = _get_trailing_pe(symbol)` so ETFs and tickers with missing fundamental data still surface a P/E when yfinance has one.

**`fundamental_screener.py`.** `CSV_OUT_FIELDS` gains `'pe_trailing'` immediately after `'svr_vs_sector_median'`. No reorder of existing columns.

**`api_server.py`.** `_inject_classifier_fields` already loads the screener row to look up `sic`; the same row is now read for `pe_trailing` with explicit `float()` coercion (defensive `try/except` around `(TypeError, ValueError)` so a malformed CSV value falls back to `None` rather than 500-ing the request).

**Tests.** `tests/unit/run_all.py` → 51 / 51 pass.

---

## Phase 3 — frontend swap (commit `1a40958`)

Removed the Sector card render block from `buildCompareCard(d)`: the 4th cell of the `svr-grid`, plus the `sect` / `ind` / `grp` / `qt` / `classLabel` / `classDetail` / `classGroup` locals that were only used to build it. The explanatory comment from Round 7c about where Sector / Industry Group / Industry come from was rewritten to a Round 7c-2 marker explaining the swap and that the classification still flows through `d.sector` / `d.industry_group` / `d.industry` and renders on the Verdict card.

Added a `fmtPE(v)` formatter and a P/E `svr-box` in the now-vacant 4th cell. Formatting rules:

- `null` / `undefined` → `—`
- `<= 0` → `—` (loss-making companies have meaningless trailing P/E)
- `> 9999` → `—` (sanity cap; flags yfinance garbage values)
- `< 100` → `value.toFixed(1) + 'x'` (e.g., `18.7x`)
- `>= 100` → `Math.round(value) + 'x'` (e.g., `234x`)

No qualifier text below the value (unlike SVR's "Expensive" / "Fair" / "Cheap" hint) — the P/E card is intentionally neutral, since it carries no color semantics or recommendation status.

Reused existing `svr-box` / `label` / `value` CSS — no new classes introduced.

---

## Sophia review #1 — request changes

Sophia reviewed after `1a40958` and approved with notes. Two structural fixes requested before docs:

1. **Font size 20px → 15px.** Skipper had matched the spec's "large value" wording with 20px (peer with SVR), but sophia argued SVR earned 20px because of color-coded thresholds, the qualifier line, and the colored card border — P/E has none of those, so 20px on P/E creates loud visual hierarchy without the supporting signals. P/E should peer with Market Cap and Quarterly Revenue (15px, `var(--text-secondary)`).
2. **Symmetric SVR-N/A.** When fundamentals are unavailable (ETFs, micro-caps without filings), the original empty-SVR branch rendered a 12px faint "SVR: N/A — ETF" pill next to the 20px P/E box. Reads as "P/E is the headline, SVR was demoted." Wrap SVR-N/A in a real `svr-box` with header "SVR" and value "—" matching the populated layout.

Sophia also noted that yfinance returns a `trailingPE` for many ETFs (SPY ~26x), which is a holdings-weighted number rather than a valuation signal — a first-time user may misread that. Recommended a `title="..."` tooltip when `isETF`. Owner accepted Touchups 1 and 2 immediately and deferred the ETF tooltip to Round 7d (natural cluster with the upcoming verdict-card layout refactor).

---

## Phase 4a — Touchup 1 (commit `9ff08ad`)

P/E card value font size 20px → 15px in both render branches (populated and empty-SVR), so the 4-card row now reads as: SVR (20px headline) / Market Cap + Quarterly Revenue + P/E (15px peer tier).

Single-line per-branch font-size change, no other edits.

---

## Phase 4b — Touchup 2 (commit `f619843`)

Empty-SVR branch rewritten from a 2-cell grid (faint pill + P/E) to a 4-cell grid mirroring the populated branch:

- SVR card now renders as a full `svr-box` with `background:#0e1e2e` (matches populated SVR identity), neutral `border:1px solid var(--bg-cell)` (no value-driven green/yellow/red coloring since there's no value), header "SVR", value "—" at 20px.
- Market Cap, Quarterly Revenue, and P/E render via `fmtCap` / `fmtPE` — both helpers already return em-dash for `null`, so a missing-fundamentals ETF gets four consistent boxes with the appropriate cells dashed-out.
- The `isETF` const used only by the old pill became dead code and was removed.

Tradeoff noted: the old pill's "ETF" / "fundamental data unavailable" qualifier is gone. Sophia and the owner agreed the consistency of the em-dash convention across all four boxes outweighs the lost contextual hint. If user feedback shows confusion about why SVR is missing, the deferred ETF-tooltip work in Round 7d will likely fold a similar hint into the SVR card too.

---

## Sophia review #2 — approved

Sophia confirmed both touchups landed cleanly:

- Populated branch: P/E at 15px peering Market Cap and Quarterly Revenue, SVR retains 20px headline. ✓
- Empty-SVR branch: 4-card grid, SVR card structurally parallel to populated, em-dash where data is missing. ✓
- `isETF` removed cleanly (zero remaining references). ✓
- Loss of "why SVR is N/A" hint: acceptable, noted in this summary above.

Two structural-parallelism wins called out: any future card added to one branch will be visibly missing from the other (maintenance dividend), and the four-card row is now visually consistent regardless of which cards have data.

---

## Deferred items (Round 7d backlog)

1. **ETF P/E tooltip** — sophia's recommendation to add `title="Weighted P/E of ETF holdings"` (or similar) to the P/E card when `quote_type === 'ETF'`. Round 7d will reintroduce an `isETF` local for this and likely the SVR card too.
2. **"Why is SVR N/A?" hint** — old pill text dropped in Touchup 2; if Round 7d adds the ETF tooltip, fold the SVR-side hint into the same pattern (`title` on the SVR card).
3. **Aria-label on em-dash values** — sophia noted that screen readers narrate `—` as "em dash" rather than "not available." Pre-existing across Market Cap and Quarterly Revenue, not a Round 7c-2 regression. Future a11y pass.

---

## Verification status

- **Tests:** `python tests/unit/run_all.py` → 51 tests, 0 failures, after each of the four feature commits.
- **DEVELOPMENT.md:** §2 repo map is a directory listing; the screener CSV column schema is not documented there in column-by-column form, so no DEVELOPMENT.md edit was needed for this round (the round prompt's §2 update was conditional on the schema being documented there).
- **CHANGELOG.md:** Round 7c-2 entry added at top of file.
- **Tree:** clean after this docs commit. No push, no merge.

### Owner verification step (required before merge to `main`)

1. Regenerate `screener_results.csv`:
   ```
   C:\Users\xkxuq\miniconda3\envs\fin\python.exe fundamental_screener.py --universe universe_prescreened.csv --csv-out screener_results.csv
   ```
2. Restart the API server, hard-refresh the browser.
3. Spot-check four ticker types on Ticker Lookup:
   - **Profitable non-override (NVDA, AAPL, MSFT)** — P/E card shows a reasonable trailing value (e.g., `30.5x`).
   - **Profitable override (GOOGL, META)** — P/E card shows a reasonable value. Override status doesn't affect P/E (P/E comes from yfinance, not classifier).
   - **Loss-making (any ticker with negative trailing earnings)** — P/E card shows `—`.
   - **ETF (SPY)** — P/E card shows yfinance's reported holdings-weighted P/E or `—` if absent.
4. Confirm Verdict Card below still shows correct Sector / Industry Group / Industry (Round 7c regression check).
5. Confirm the four-card row layout is balanced both for tickers with fundamental data and for those without (compare an SVR-populated symbol against an ETF or data-unavailable symbol).

---

## Files touched (Phases 2-4b + this docs commit)

- `fundamental_metrics.py` (+15 lines: `_get_trailing_pe` helper + 2 emission sites + cache)
- `fundamental_screener.py` (+1 column in `CSV_OUT_FIELDS`)
- `api_server.py` (+10 lines: `pe_trailing` lookup + float coercion in `_inject_classifier_fields`)
- `frontend/index.html` (P/E card render + `fmtPE` helper, then two touchups; net +14 / -33 vs pre-round)
- `round7c-2-summary.md` (this file, new)
- `CHANGELOG.md` (Round 7c-2 entry added)
