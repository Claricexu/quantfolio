# How to Define a Good Firm

A straightforward framework for identifying industry leaders and hidden gems, distilled from Nanalyze's research methodology across 49 newsletters.

> **Quantfolio integration:** this framework ships as `fundamental_screener.py` + `fundamental_metrics.py`, with **archetype-dispatched** tests (Phase 1.9, 2026-04-18). The runtime classifies each ticker as **GROWTH** or **MATURE** on a single Revenue-YoY cut, then applies a tailored 5-test rubric and per-archetype dealbreakers to emit one of four verdicts: **LEADER / GEM / WATCH / AVOID**. See the *Archetype Routing* section below for the current shape; the original Nanalyze core principles (Core Philosophy, the 5 business tests, SVR valuation, dealbreaker list) stay intact as the underlying business ideas.

---

## Core Philosophy

> **"Invest in companies, not stocks."**

Ignore daily price noise. Focus on the quality of the underlying business. Holding periods are measured in **decades, not months**.

---

## The 5 Tests of a Good Company

A firm must pass **all five** tests to be considered a "good" investment candidate. Failing any one is usually a dealbreaker.

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

| Pattern | Example |
|---|---|
| SPACs, especially de-SPACed names | Avg -50% to -70% since 2009 |
| Penny stocks / microcaps (< $100M) | Algorhythm/RIME |
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
- `burning_cash`: `flag_burning_cash = True` (FCF < 0 AND runway < 24mo)

`flag_diluting` is deliberately **not** a GROWTH dealbreaker — SaaS stock-based-comp creep trips NOW / NFLX / PANW legitimately without indicating imminent harm. MATURE enforces capital discipline through `stability` + `diluting` dealbreaker; GROWTH shuts down only on actual cash-burn risk. `flag_spac_or_microcap` dropped entirely because Phase 1.0 already floors the universe at $1B market cap.

### Verdict Mapping (both archetypes)

| Verdict | Condition |
|---|---|
| `LEADER` | 5/5 tests pass **AND** `market_cap_rank_in_sector ≤ 5` **AND** no dealbreaker |
| `GEM` | 5/5 tests pass **AND** `market_cap_rank_in_sector > 5` (or rank unknown) **AND** no dealbreaker |
| `WATCH` | 3–4/5 tests pass **AND** no dealbreaker |
| `AVOID` | ≤ 2/5 tests pass **OR** any dealbreaker |
| `INSUFFICIENT_DATA` | archetype = UNKNOWN **OR** < 3 tests returned a non-null result (ETFs, ADRs, ingestion gaps) |

The LEADER / GEM split is a **sector-rank tiebreak at 5/5**, not a quality gate: both are business-quality peaks, just at different company sizes. That's why `leader_selector.py` emits `leaders.csv` as `all LEADER ∪ top GEM by good_firm_score until the 100-row cap is hit`.

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
- **`leader_selector.py`** — translates verdicts into `leaders.csv` (all LEADER ∪ top GEM by `good_firm_score` until total = 100).

---

## Quick Reference Card

**A good firm has:**
1. Double-digit revenue growth, sustained
2. Recurring revenue, high margins
3. Positive operating cash flow
4. #1/#2 position or unique proprietary asset
5. SVR ≤ 18 (lower is better)

**A real gem has all five, plus:**
- An unloved story (market is missing something)
- Insiders buying
- Improving — not just high — metrics

**Buy signal:** Good firm + short-term market panic drops SVR below historical average.
**Sell signal:** SVR balloons above 18 OR any of the 5 core tests fail for 2+ quarters.
