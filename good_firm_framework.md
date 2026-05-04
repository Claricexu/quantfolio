# How to Define a Good Firm

A straightforward framework for identifying high-quality businesses, distilled from Nanalyze's research methodology across 49 newsletters.

> **Quantfolio integration:** this framework ships as `fundamental_screener.py` + `fundamental_metrics.py`, with **archetype-dispatched** tests (Phase 1.9, 2026-04-18) and a size-blind verdict (Round 9a, 2026-05-03). The runtime classifies each ticker as **GROWTH** or **MATURE** on a single Revenue-YoY cut, then applies a tailored 5-test rubric and per-archetype dealbreakers to emit one of four verdicts: **LEADER / WATCH / AVOID / INSUFFICIENT_DATA**. See the *Archetype Routing* section below for the current shape; the original Nanalyze core principles (Core Philosophy, the 5 business tests, SVR valuation, dealbreaker list) stay intact as the underlying business ideas.

---

## Core Philosophy

> **"Invest in companies, not stocks."**

Ignore daily price noise. Focus on the quality of the underlying business. Holding periods are measured in **decades, not months**.

---

## The 5 Tests of a Good Company

A firm must pass **all five** tests to earn the top-tier `LEADER` verdict. Passing only 3–4 routes to `WATCH` (worth tracking but not a quality peak); passing ≤ 2 routes to `AVOID`. The five tests are quality dimensions, not independent kill-switches — a single failure isn't automatically fatal because the *aggregate* test count is what drives the verdict. The dealbreakers in the *Archetype Routing* section below are the actual hard cutoffs that bypass the test count.

### 1. Growth — Is revenue compounding fast?

| Metric | Pass | Fail |
|---|---|---|
| **Annual revenue growth** | ≥ 10% (disruptive tier) | < 10% for a theme that's supposed to be "disruptive" |
| **Growth direction** | Stable or accelerating | Decelerating for 2+ years |
| **Revenue quality** | Organic and reported clearly | Inflated by bitcoin sales, acquisitions, or one-time items |

**Dealbreakers:** Single-digit growth in a "disruptive" category (UiPath ~7%, AvidXchange 4%, Infineon negative). Pre-revenue SPACs. Companies that repeatedly miss their own guidance.

---

### 2. Business Model — Is it scalable and recurring?

**Preferred (in order):**
1. **SaaS / Software** — recurring revenue, ~70%+ gross margins, high lifetime value
2. **Platform with network effects** — proprietary data that compounds (e.g. Tempus genomics dataset)
3. **Mission-critical infrastructure** — switching costs, long contracts (cybersecurity, EDA software)

**Avoid:**
- Hardware-only businesses (low margin, no recurring revenue)
- "Race to the bottom" margins (PayPal's take rate)
- Cyclical industries without secular growth (auto semis, industrial chips)
- Biotech with no revenue or clinical-stage pivots (AbCellera pivot was a "death sentence")

---

### 3. Profitability — Is the path to cash real?

| Metric | Target |
|---|---|
| **Gross margin** | ≥ 70% for SaaS; improving trend for others |
| **Operating cash flow** | Positive (preferred over EBITDA — easier to fake EBITDA) |
| **Cash burn trend** | Declining; no ongoing shareholder dilution |
| **Rule 40** (growth % + profit margin %) | ≥ 40% indicates a healthy SaaS |

**Red flag:** If a company says "$X million in available liquidity" but keeps burning cash, expect dilution.

---

### 4. Moat & Market Position — Does the lead compound?

**Must have one of:**
- **#1 or #2** in the category (ISRG in surgical robots, ASML in EUV lithography, NVIDIA in AI chips)
- **Proprietary dataset** that competitors can't replicate (Tempus 9M genomes)
- **Switching costs** — enterprise customers entrenched (ServiceNow with 86% of Fortune 500)
- **Regulatory or patent moat** — real intellectual property

> **"The next NVIDIA is NVIDIA."** — Don't bet on the challenger when the leader is still growing.

**Red flags:** Over-reliance on one customer (Palantir/US gov, Figure/HELOCs), fighting entrenched giants with deep data (Lemonade/Root vs Progressive).

---

### 5. SaaS-Specific Retention Metrics

If it's SaaS, two numbers tell you everything:

| Metric | What it means | Healthy | Warning |
|---|---|---|---|
| **Net Retention Rate (NRR)** | Are customers spending more each year? | ≥ 120% | < 110% |
| **Gross Retention Rate (GRR)** | Are customers leaving? | ≥ 97% | < 95% |
| **Remaining Performance Obligations (RPO)** | Contractually committed future revenue | Growing | Flat/shrinking |

---

## Valuation — When to Buy

Growth and quality are **not enough** — price still matters.

### Simple Valuation Ratio (SVR)

```
SVR = Market Cap / Annualized Revenue
```

| SVR | Verdict |
|---|---|
| < 3 | Undervalued — dig deeper for catalysts |
| 3 – 7 | Fair value — Nanalyze catalog average is ~7 |
| 7 – 18 | Expensive but acceptable for top-tier quality (ISRG, MSFT) |
| ≥ 18 | Too expensive — wait for a pullback |
| ≥ 21 | Won't touch it |

**Key insight:** Quality companies rarely trade cheap. The strategy is to **wait for market overreactions** (DeepSeek panic, software-stocks-sinking panics, earnings-day crashes) and buy quality on sale. Microsoft -25% in Q1 2026 = opportunity, not threat.

### When SVR doesn't apply

Use **gross profit multiples** instead of revenue multiples when:
- Revenue includes pass-through items (Block's bitcoin = 40% of revenue, 4% of gross profit)
- Take rates are collapsing (payment processors)

---

## Instant Dealbreakers (The "Avoid List")

> ⚠ **Aspirational — not enforced in code.** The patterns below describe the kind of business profile a human reader should reflexively reject. The shipped screener does **not** automatically detect any of them. The two automated dealbreakers it actually enforces are listed under *Archetype Routing → MATURE / GROWTH dealbreakers* below (`cagr_shrinking`, `diluting`, `burning_cash`). Closing those gaps — SPAC-history detection, going-concern flag parsing, dilution intensity, etc. — is the subject of a separate methodology project queued ahead of the next quarterly SEC fetch (5/15).

| Pattern | Example |
|---|---|
| SPACs, especially de-SPACed names | Avg -50% to -70% since 2009 |
| Penny stocks / microcaps (< $100M) | Algorhythm/RIME (universe pre-screen floors at $1B, so this won't appear in shipped output) |
| Pre-revenue tech with no product | AbCellera pivot, Wolfspeed pre-bankruptcy |
| Shareholder dilution as a business model | Unprofitable biotechs with perpetual secondary offerings |
| "AI" bolt-on with no real AI revenue | Companies adding "AI" to their name |
| Non-profitable tech in speculative frenzies | Goldman Sachs Non-Profitable Tech Index peaks |
| Insurance with loss ratio > 59% (industry avg) | Lemonade's loss ratio > 100% in early years |
| Crypto tokens that aren't Bitcoin (beyond 1-2% of portfolio) | Memecoins |

---

## The Two-Tier Portfolio (Nanalyze's own structure)

Inspired by how Nanalyze runs their own money:

| Tier | Purpose | Rules |
|---|---|---|
| **Core (80% of capital)** | Wealth preservation, compound dividends | Quality dividend growers, held "as if the market closed for a decade" (Quantigence methodology) |
| **Disruptive (20% of capital)** | Alpha from innovation | Max 5% per position; diversified across themes; pass all 5 tests above |

---

## Archetype Routing (Phase 1.9)

The 5 business tests above describe *what* to look for. The Quantfolio runtime had to pick *how hard* to score each test — and the honest answer is "it depends on the company." Holding KO / JNJ / WMT to the same double-digit revenue-growth bar as NOW / CRWD is how a screener ends up flagging every mature cash-cow as AVOID. The fix (locked 2026-04-18) is a **binary archetype classifier** that routes each ticker through one of two tailored rubrics.

**Classifier** (`fundamental_screener.py:classify_archetype`, T = 0.12):

```
if revenue_yoy_growth is None:
    archetype = UNKNOWN   # ETFs, ADRs, ingestion gaps — no rubric applied
elif revenue_yoy_growth >= 0.12:
    archetype = GROWTH    # reward revenue pace + path to profits
else:
    archetype = MATURE    # reward cash throw-off + capital discipline
```

The 12% threshold was empirically locked via `diag_threshold_sensitivity.py` against anchor sets (KO / JNJ / PG / WMT / MCD must land MATURE; NVDA / MSFT / META / CRWD / NOW must land GROWTH).

### MATURE Rubric (`fundamental_screener.py:MATURE_TESTS`)

| # | Test | Pass if | Rationale |
|---|---|---|---|
| 1 | `not_declining` | `revenue_3y_cagr ≥ 0%` | Mature isn't punished for slow growth — only for shrinking over 3y |
| 2 | `margin_quality` | `operating_margin_ttm ≥ 10%` | Real operating leverage, industrial-median benchmark |
| 3 | `cash_generation` | `ocf_ttm > 0 AND fcf_margin_ttm ≥ 8%` | The core MATURE thesis — cash throw-off |
| 4 | `moat` | `roic_ttm ≥ 10% OR market_cap_rank_in_sector ≤ 5` | ROIC above WACC, or scale-moat fallback |
| 5 | `stability` | `trajectory ≠ decelerating AND flag_diluting = False` | Consistency + capital discipline |

**MATURE dealbreakers** (any → AVOID, bypass test count):
- `cagr_shrinking`: `revenue_3y_cagr < -5%` (terminally declining, not merely slow)
- `diluting`: `flag_diluting = True` (share count up > 15% in 3y, split-adjusted)

### GROWTH Rubric (`fundamental_screener.py:GROWTH_TESTS`)

| # | Test | Pass if | Rationale |
|---|---|---|---|
| 1 | `growth_rate` | `yoy ≥ 12% AND (not decelerating OR yoy > 30%)` | 12% floor **aligned with classifier cut** (Phase 1.9c, 2026-04-20); 30% bypass is the NVDA rule (hyper-growers comp down but remain growth-phase) |
| 2 | `unit_economics` | `gross_margin_ttm ≥ 50%` | Software / pharma / premium-brand line |
| 3 | `path_to_profits` | `ocf_ttm > 0 OR rule_40_score ≥ 40` | Cash-positive today, or on the efficient frontier |
| 4 | `moat` | `roic_ttm ≥ 10% OR market_cap_rank_in_sector ≤ 5` | Shared with MATURE |
| 5 | `capital_efficiency` | `rule_40_score ≥ 40` | Efficient-frontier line (tightened from Path-A's ≥ 25) |

**GROWTH dealbreakers** (any → AVOID):
- `burning_cash`: `flag_burning_cash = True`, where `flag_burning_cash = (ocf_ttm < 0)` — a single-period TTM operating cash flow check, **not** the FCF-plus-runway formulation an earlier draft of this doc described. The simple TTM check is intentional: SEC filings arrive semi-annually, so a multi-period FCF/runway calculation would lag a real cash-flow inflection by 6–12 months. The TTM check trips fast enough to actually catch a deteriorating GROWTH name before it dilutes.

`flag_diluting` is deliberately **not** a GROWTH dealbreaker — SaaS stock-based-comp creep trips NOW / NFLX / PANW legitimately without indicating imminent harm. MATURE enforces capital discipline through `stability` + `diluting` dealbreaker; GROWTH shuts down only on actual cash-burn risk. `flag_spac_or_microcap` dropped entirely because Phase 1.0 already floors the universe at $1B market cap.

### Good Firm Score Formula

The verdict is the headline label; `good_firm_score` is the continuous tiebreaker used by `leader_selector.py` to rank LEADER rows when filling `leaders.csv`. Computed in `fundamental_screener.score_ticker`:

```
score = passes * 15                                  # 0–75 from the 5 tests
      + 10  if known > 0 and not any_dealbreaker     # data-coverage / clean-record bonus
      +  5  if roic_ttm  >= 0.20                     # ROIC quality bonus
      +  5  if rule_40_score >= 40                   # Rule-40 quality bonus
```

Theoretical maximum: **95** (5 × 15 + 10 + 5 + 5). Round 9a removed an artificial `min(score, 100)` cap that was dead code — the cap was never reachable.

### Verdict Mapping (both archetypes)

| Verdict | Condition |
|---|---|
| `LEADER` | 5/5 tests pass **AND** no dealbreaker |
| `WATCH` | 3–4/5 tests pass **AND** no dealbreaker |
| `AVOID` | ≤ 2/5 tests pass **OR** any dealbreaker |
| `INSUFFICIENT_DATA` | archetype = UNKNOWN **OR** < 3 tests returned a non-null result (ETFs, ADRs, ingestion gaps) |

Round 9a (2026-05-03) collapsed the prior LEADER/GEM split. Pre-9a the schema gated 5/5 winners on `market_cap_rank_in_sector ≤ 5` (LEADER) vs. `> 5` (GEM); both were the same quality tier but the verdict label encoded company size. With the split removed, verdicts encode pure quality; size context still rides on the row via `market_cap_rank_in_sector` (used by the moat-fallback test and surfaced in the Leader Detector table) but no longer steers the verdict label.

---

## Forensic Flags (Round May 15)

A **separate layer from the verdict.** The verdict (LEADER/WATCH/AVOID/INSUFFICIENT_DATA) encodes pure business quality — Round 9a's invariant. Forensic flags ride alongside on `forensic_flags_json` + `forensic_flag_count` and warn about hidden accounting fragility on otherwise-good firms without modifying the verdict label. A 5/5 LEADER with a forensic flag is still a LEADER; the flag just means "look harder before sizing this position."

The flags are written to `screener_results.csv` by `fundamental_screener._compute_forensic_flags`, surfaced as amber chips on the Verdict Card, and consumed by `leader_selector.py` to exclude flagged rows from `leaders.csv` (so Layer 2's training set isn't polluted by accounting outliers).

### The three working flags

| Flag | Threshold | What it means |
|---|---|---|
| `ni_ocf_divergence` | NetIncome > OperatingCashFlow for **3 consecutive fiscal years** (newest-first, strict `>` per year) | Reported profit has outrun cash collected. Sometimes a sign of aggressive accounting (working-capital build, accrual extensions, channel stuffing); worth a closer look at receivables and accruals. |
| `leverage_high` | **Net Debt / EBITDA > 4x AND interest coverage < 2x** (AND threshold; both legs required) | Capital structure is stretched and earnings barely cover interest. Limited room if rates rise or business slows. EBITDA is approximated as OperatingIncomeLoss alone today (D&A is not in `XBRL_TAG_CHAINS` yet — strictly conservative for this check, makes the ratio more likely to trip). |
| `dilution_velocity` | Shares-outstanding YoY growth > **10%** in the most recent year (strict `>`, not `>=`) | One-year burst of share issuance. Complements the existing `flag_diluting` dealbreaker (which is 15% over 3 years and can miss a single-year spike that resets to baseline). |

### Sector exclusions (SIC 6000–6799)

Banks, insurance carriers, and REITs are excluded from the entire forensic-flag layer because the flagged behaviours are normal under their accounting frameworks — an insurer's float-investment dynamics structurally produce NI > OCF, and a bank's leverage ratios live in a regime where Net Debt / EBITDA isn't a meaningful comparison. The exclusion is by SIC range:

- SIC 6000–6299 — banks, brokers, financial holding companies
- SIC 6300–6499 — insurance carriers and agents
- SIC 6500–6799 — real estate, investment, REITs

Excludes ~all of SEC SIC division H (Finance / Insurance / Real Estate). Done by raw SIC range rather than classifier triple because the classifier's `industry_group` for banks ("Capital Markets") collides with REITs and with the V/MA payment-network overrides — the SIC range is the canonical boundary on the actual filer's accounting framework.

### Going Concern (deferred)

A fourth flag — `going_concern` — was originally in scope for this round and is **deferred pending 10-K text-parsing infrastructure**. The honest gap:

- The canonical us-gaap tag `SubstantialDoubtAboutGoingConcern` does **not** appear in SEC's companyfacts/frames API for known going-concern filers. Empirical probe of nine such filers (BIG, AMC, Wheels Up, RAD, BBBY, PRTYQ + three Cat-A failures) found zero exposure under the companyfacts JSON, even though every one of those filers carried explicit going-concern language in their Auditor's Report.
- SEC files the signal as **narrative text in 10-K Item 8 (Auditor's Report)**, not as a structured XBRL Boolean fact. The companyfacts API surfaces structured facts only — text blocks aren't exposed.
- Closing the gap requires a **10-K text-parsing pipeline** that downloads filing HTML and reads the Auditor's Report, keying off the standard PCAOB phrase "substantial doubt about [the company's] ability to continue as a going concern" plus a couple of common variants. That's a separate project; tracked in [FEATURE_BACKLOG.md](FEATURE_BACKLOG.md).

The current behaviour is therefore **always False** — `_flag_going_concern` reads `m.get('going_concern_present')`, and the metrics layer hardcodes that field to False. The schema slot ships anyway: the flag appears in `forensic_flags_json` on every non-sector-excluded row, the override CSV accepts `flag_name=going_concern`, and the frontend has a chip label and tooltip wired in. When the text-parser lands and starts feeding True values into `going_concern_present`, no consumer changes are required — the chip starts rendering, leader_selector starts excluding flagged rows, and overrides for the flag start applying.

This is intentional: the schema is forward-compat, the gap is honest, and the user knows not to trust this layer for going-concern detection in the meantime.

### Override path

False positives are inevitable on a flag set this sharp (NVDA, for example, correctly trips `ni_ocf_divergence` because of stock-based-compensation timing, but the underlying business is fine). The override path lets an operator suppress a specific `(symbol, flag_name)` until a chosen expiry date without changing detection logic.

**File:** `cache/forensic_flag_overrides.csv` (gitignored is overridden — the file ships with a comment-only template).

**Schema:**

```
symbol,flag_name,expires_at,reason
NVDA,ni_ocf_divergence,2027-01-01,SBC-heavy NI conversion lag is real but not forensic; revisit Q1 2027
```

| Column | Meaning |
|---|---|
| `symbol` | Uppercase ticker (matches `fundamentals.db` symbol) |
| `flag_name` | One of `ni_ocf_divergence`, `leverage_high`, `going_concern`, `dilution_velocity` |
| `expires_at` | ISO date YYYY-MM-DD; override is active iff `today < expires_at` |
| `reason` | Free text; surfaced in audit logs and (future) UI tooltip |

**Loader semantics** (`fundamental_screener._load_forensic_flag_overrides`):

- Lines starting with `#` are treated as comments — the schema example sits inline without polluting the override map.
- Malformed rows (bad date, missing column, unknown flag) are skipped with a stderr warning rather than raising — a typo in this file shouldn't break the screener for the entire universe.
- Read **once per process** at first access and cached at module level. Quarterly rebuild cadence makes this race-free; tests can call `reset_forensic_overrides_cache()` to reset between scenarios.

**Effect on a flagged row** when an override is active:

- The flag stays `True` in `forensic_flags_json` so the UI can render an "overridden" chip (dashed border, 0.55 opacity, " · muted" suffix on the label) — full transparency about which flags are present-but-suppressed.
- The flag is excluded from `forensic_flag_count`, which is what `leader_selector` reads for pool eligibility — so the row is eligible for `leaders.csv` again.

When the expiry passes, the override silently stops applying — the flag starts counting again on the next quarterly rebuild without any code change.

### Pre-1.9 Pseudocode (historical — retired)

The original monolithic screen (no archetype routing, 5-way verdict with `HIDDEN_GEM` / `INDUSTRY_LEADER`) was replaced when it structurally under-counted mature cash-cows as AVOID. It's kept here as context for why the split exists:

```text
[ARCHIVED — replaced 2026-04-18]
SCREEN_RESULT:
    GOOD_FIRM = GROWTH_OK AND PROFITABILITY_OK AND MOAT_OK AND VALUATION_OK AND SAFETY_OK
    HIDDEN_GEM = GOOD_FIRM AND (svr <= industry_median) AND (analyst_coverage <= LOW)
    INDUSTRY_LEADER = GOOD_FIRM AND (market_cap_rank_in_industry <= 2)
```

---

## Known Unfixable & Phase 2 Backlog

The MATURE/GROWTH split is intentionally a first-cut dispatcher — "two bars beats one" — not a final answer. Several business profiles honestly fail the current rubrics despite being defensible investments; they're logged for a future CYCLICAL / UTILITY sub-archetype pass:

- **Cyclicals** (CVX, XOM, F, GM, UPS, NKE, TGT) — revenue dips through commodity or consumer cycles trip MATURE's `cagr_shrinking` dealbreaker (`revenue_3y_cagr < -5%`) even during normal trough years.
- **Regulated utilities** (DUK, NEE, SO) — fail `cash_generation` because regulated capex suppresses FCF margin below 8%, despite stable earnings and dividend aristocrat status.
- **CostOfServices chain gap** (~10% of $1B+ service-dominant tickers — TMO, SLB, CB, GD, GM, GE, LNG, AIR, NEE) — null `gross_margin_ttm` because segment-specific cost tags aren't in the current XBRL chain. Suppresses mid-cap LEADER candidates but produces no false positives.
- **`dividend_yield` yfinance unit inconsistency** (Phase 1.9a) — WMT reports 78%, MSFT 86%, CRM 97% at face value. No current test consumes `dividend_yield`, so this is dormant; blocks any future dividend-based refinement of the classifier.

These are tracked in the planning notes, not in the shipped rubric.

---

## Pipeline Touchpoints

- **`universe_builder.py`** — Phase 1.0/1.1 pre-screen (SEC ~10k → 1,414 survivors by liquidity + filing history + SIC exclusion + SVR sanity + finance-sector cap). Only rows that pass enter the Good Firm Framework.
- **`edgar_fetcher.py`** — SEC XBRL fact ingest (90-day TTL, walked across paginated `filings.files[]`). Source of every metric the rubric reads.
- **`fundamental_metrics.py`** — computes the 15 metrics referenced above (margins, CAGR, FCF, ROIC, Rule-40, SVR, etc.).
- **`fundamental_screener.py`** — classifier + dispatch + verdict (this doc's spec, in code).
- **`leader_selector.py`** — translates verdicts into `leaders.csv` (top-N LEADER by `good_firm_score`, target_size=100, under-fill if fewer LEADERs exist — WATCH is intentionally not eligible since it represents 3–4/5 tests passed, not a quality peak).

---

## Quick Reference Card

**A good firm has:**
1. Double-digit revenue growth, sustained
2. Recurring revenue, high margins
3. Positive operating cash flow
4. #1/#2 position or unique proprietary asset
5. Reasonable valuation — see *Valuation* section above for SVR / gross-profit-multiple guidance (Round 7d removed SVR-vs-sector bonus from the score; SVR remains a human reading aid, not an automatic kill switch)

**A standout candidate has all five, plus the qualitative tells:**
- An unloved story (market is missing something)
- Insiders buying
- Improving — not just high — metrics

**Buy signal:** Good firm + short-term market panic drops valuation below historical average.
**Sell signal:** Multiple core tests slipping for 2+ quarters, or a dealbreaker triggers (e.g. shrinking 3y CAGR, dilution > 15%, OCF turns negative).
