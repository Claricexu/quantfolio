# C-3 Phase 2 Design — BacktestEngine

Status: DRAFT, pending wright review. No code yet. Nothing in `finance_model_v2.py` changes in this
phase.

Context: Phase 1 captured 28 baseline JSONs (7 tickers x 4 paths). The baselines show one
meaningful cross-path divergence — **MSFT**: `backtest_symbol` final = `$92,159.04` vs
`backtest_multi_strategy` final = `$87,367.59` (both on the `full` strategy, on the same
engineered features, on the same walk-forward schedule). NVDA, despite having similar
fat-tailed growth, does **not** drift (both = `$1,807,284.34`). Five of seven tickers agree
byte-for-byte. The MSFT delta is attributable to a single fact:

- `predict_ticker` and `backtest_symbol` call `build_stacking_ensemble` (OOF-trained).
- `backtest_multi_strategy` calls `build_stacking_ensemble_fast` (val-MAE weighted).

The Phase 2 engine unifies the three loops. Phase 4 will assert byte-identical output against
**one** of the callers; the other will change behavior on MSFT (and on any future ticker where
the two ensembles disagree). That change is a bug-fix (inconsistent builder selection across
paths), not a regression. `round3-summary.md` will document this explicitly.

---

## 1. Module location and name

Proposal: **`backtest_engine.py` at the repo root**, peer to `finance_model_v2.py`.

Why:
- It is called by `finance_model_v2.py` functions, not the other way around — co-locating it
  at the root keeps the import cycle clean (`finance_model_v2` imports from
  `backtest_engine`, never the reverse).
- `api_server.py` continues to import only from `finance_model_v2`; the engine is an
  implementation detail of the three callers. Nothing in `api_server.py` needs to import from
  `backtest_engine`.
- Placing it under `tests/` would be wrong — this is production code exercised by production
  API paths.
- Placing it in a sub-package (e.g., `engine/backtest_engine.py`) would force a package
  `__init__.py` and a heavier import discovery change. Not worth it for one module.

File expected to be ~300-400 LOC; deletion of the unchosen ensemble builder in Phase 5
removes roughly 20 LOC from `finance_model_v2.py`.

---

## 2. Public API

All three dataclasses live in `backtest_engine.py` and are importable by name.

### `BacktestConfig` (frozen dataclass)

```
symbol: str
initial_cash: float = 10000.0
threshold: float = THRESHOLD                 # from finance_model_v2, z-score trigger
zscore_lookback: int = ZSCORE_LOOKBACK       # rolling window
min_zscore_samples: int = MIN_ZSCORE_SAMPLES # hard gate for signal activation; default 20
retrain_freq_days: int | None = None         # None = one-shot (predict_ticker style)
min_train_days: int = MIN_TRAIN_DAYS
seed_from_validation: bool = True            # seeds pred_history with final zscore_lookback
                                             # vals from the validation fit
random_state: int = 42
ensemble_builder: Literal['oof', 'fast'] = 'oof'  # see section 6
feature_version: Literal['v2', 'v3'] = 'v3'
```

Configs are frozen so the Engine can hash them for a run manifest (see section 9).

### `BacktestResult` (frozen dataclass)

Fields are a **superset** of the JSON fields that Phase 1 captured.

```
# Summary stats (match capture_baselines.py schema exactly)
final_portfolio_value: float
sharpe: float
sortino: float
max_drawdown_pct: float
num_trades: int
buys: int
sells: int
holds: int

# Time series (same length = period_days)
dates: list[str]                    # 'YYYY-MM-DD'
portfolio_curve: list[float]        # equity at each step
per_period_returns: list[float]     # pct_change of portfolio_curve
signals: list[Literal['BUY','SELL','HOLD']]
raw_predictions: list[float]        # NEW — wright's explicit ask. Model output BEFORE
                                    # z-score transform, one per step (NaN before seed fills).
z_scores: list[float]               # NEW — alongside raw_predictions; 0.0 before
                                    # min_zscore_samples is reached

# Trades (as in capture_baselines.py)
trades: list[Trade]   # Trade = {date, signal, price, z_score, portfolio_value}

# Metadata
symbol: str
strategy: str                       # 'full' | 'buy_only' | 'buy_hold'
period_days: int
start_date: str
end_date: str
initial_cash: float
ensemble_builder: str               # 'oof' | 'fast'
min_zscore_samples_used: int
min_train_days_used: int
retrain_freq_days: int | None
feature_version: str
config_hash: str                    # sha256 of BacktestConfig fields; Phase 9 manifest use
```

### `BacktestEngine`

```
class BacktestEngine:
    def __init__(self, config: BacktestConfig, data: pd.DataFrame): ...
        # data = engineered-features frame; MUST already have gone through
        # engineer_features_v3 (or v2). Engine does NOT fetch or engineer.
        # This keeps the engine pure and testable with synthetic frames.

    def run(self, strategy_fn: Callable[[Context], Signal]) -> BacktestResult: ...
        # Single-strategy run. The three current callers collapse to run() invocations.

    def run_multi(self, strategy_fns: dict[str, Callable]) -> dict[str, BacktestResult]: ...
        # Multi-strategy run for backtest_multi_strategy: same walk-forward loop,
        # same predictions, N portfolios. Outputs dict keyed by strategy name.
```

`run_multi` is the only way to guarantee that two strategies see literally the same
`raw_predictions[i]`. Calling `run` twice cannot — retrains and the RandomForest seed
path re-enter, and floating-point OOF noise can propagate. `run_multi` is the byte-identical
path for `backtest_multi_strategy`.

---

## 3. Private state (per timestep)

The engine tracks exactly one set of "global" walk-forward state, shared across all strategies
in `run_multi`:

- `model`: current ensemble (rebuilt every `retrain_freq_days` steps, or None = one-shot)
- `scaler`: `StandardScaler` fit on train slice only (no val leakage — matches current code)
- `retrain_counter: int`
- `pred_history: list[float]` — rolling, capped at `zscore_lookback`
- `step_idx: int`

Per-strategy state (one struct per entry in `run_multi`'s `strategy_fns`):

- `cash: float`
- `shares: float`
- `portfolio_curve: list[float]`
- `signals: list[str]`
- `trades: list[dict]`

The engine never mixes strategies' portfolio state. Strategies never see each other.

---

## 4. Strategy callback contract

```
@dataclass(frozen=True)
class Context:
    z_score: float           # already computed by engine. 0.0 if samples < min_zscore_samples.
    raw_pred: float          # current step's model output
    price: float
    date: str
    position_shares: float   # strategy's current holding
    cash: float              # strategy's current cash
    samples_ready: bool      # True iff len(pred_history) >= min_zscore_samples
    threshold: float         # passthrough from config

Signal = Literal['BUY', 'SELL', 'HOLD']
```

Contract: `strategy_fn(ctx) -> Signal` is a **pure function of ctx**. The engine applies the
signal to the strategy's cash/shares afterward. Strategies cannot mutate pred_history,
cannot force retrains, cannot skip steps.

Three built-in strategies ship with the engine:

- `full_signal(ctx)`: BUY if `z >= threshold` and cash > 0; SELL if `z <= -threshold` and
  shares > 0; else HOLD.
- `buy_only(ctx)`: BUY if `z >= threshold` and cash > 0; else HOLD. Never sells.
- `buy_hold(ctx)`: BUY on first step where cash > 0; else HOLD. Ignores z entirely.

Edge: if `ctx.samples_ready` is False, every built-in returns HOLD regardless of z (which
will be 0.0 anyway). User-supplied strategies must honor the same invariant; the engine does
NOT force this — it is a contract, enforced by the built-ins and by strategy-fn unit tests,
not by runtime assertion. (Rationale: a custom strategy may want to act on raw_pred before
seeding — rare, but we should not preclude it. Tests will assert the built-ins.)

---

## 5. MIN_ZSCORE_SAMPLES=20 enforcement

Single entry point, always on, applied inside the engine **before** `strategy_fn` is called.

The engine computes `z_score`, `raw_pred`, and `samples_ready` for step `i`, then constructs
`Context`. There is no code path by which `strategy_fn` runs without this guard being
evaluated first.

Current code has the check duplicated across lines 487-493, 784-789, and 878-883 of
`finance_model_v2.py`. After Phase 3, all three paths hit the one engine guard. Phase 4
asserts byte-identical output, so the duplication-removal cannot regress behavior on the
paths that already respect the guard. `predict_ticker`'s inline loop uses an adaptive
`seed_n=min(20,max(5,len(Xvl)//3))` that is NOT the same as `MIN_ZSCORE_SAMPLES` — that
seed is about bootstrapping pred_history, not gating signals. The engine keeps these
concerns separate: `seed_from_validation` controls the former, `min_zscore_samples`
controls the latter.

---

## 6. LOAD-BEARING: Ensemble choice

### Decision: default to `build_stacking_ensemble` (OOF-trained, slow)

Rationale: Two of three current callers (`predict_ticker` at L470, `backtest_symbol` at L770)
already use the OOF builder, which is also the path that populates the `/api/predict` and
`/api/report` responses users actually see. The fast builder is used only in
`backtest_multi_strategy` (L861). Defaulting to OOF means the engine's default behavior
matches the user-facing prediction path, and the one outlier gets pulled into line.

The MSFT drift ($92,159 vs $87,367, 5.5% spread on 10 years) is the observable cost of the
current split. Phase 4 target is **byte-identical against `backtest_symbol`** (OOF). This
means:

- `predict_ticker` → engine(ensemble='oof'): byte-identical (same builder, same data).
- `backtest_symbol` → engine(ensemble='oof'): byte-identical target. Asserted in Phase 4.
- `backtest_multi_strategy` → engine(ensemble='oof'): **will change output on MSFT** from
  `$87,367.59` to `$92,159.04`. No change on SPY/JNJ/KO/AAPL/NVDA. SHORTHIST_AAPL may shift
  slightly given the short-history path; the baseline is preserved in git for before/after
  comparison.

Speed trade-off (goes in engine module docstring):

> The OOF builder runs ~18 model fits per retrain (6 base models x 3 OOF folds) vs the fast
> builder's ~6 fits (base models once, val-MAE-weighted stacking). On a 10-year backtest with
> retrain_freq_days=63 that is ~44 retrains, so the engine spends ~800 fits (OOF) vs ~270
> fits (fast). Wall-clock delta on my machine: ~3-4x. For CLI backtests this is tolerable.
> For `/api/backtest` served synchronously, we may want `ensemble_builder='fast'` as a
> config override, pending wright's call. The engine supports both; only the default is OOF.

### The unchosen builder

`build_stacking_ensemble_fast` is DELETED in Phase 5 unless an `/api/backtest` latency
regression surfaces during Phase 4 sign-off. If it does, the fast builder survives as an
opt-in `config.ensemble_builder='fast'` path and only the `backtest_multi_strategy` caller
keeps using it. This will be a Phase 4 follow-up, not a Phase 2 commitment.

### `round3-summary.md` note

Summary text must include: "Phase 3 bug-fix: backtest_multi_strategy previously used
build_stacking_ensemble_fast, causing MSFT divergence from predict_ticker and
backtest_symbol. After the engine refactor, all three callers use build_stacking_ensemble
(OOF). MSFT/full final portfolio changes from $87,367.59 to $92,159.04 on the 10-year
walk-forward; other tickers unchanged."

---

## 7. Edge cases — one-line invariants

- **Empty predictions** (zero rows after `dropna`): engine raises `ValueError("insufficient
  data: 0 feature rows")`. Callers currently return None silently — Phase 3 will let that
  propagate as the same ValueError up to `/api/backtest`, which already has a try/except
  wrapper.
- **Single-period data**: engine runs the loop zero times (no step where i < len-1). Stats
  output has `period_days=0`, `portfolio_curve=[initial_cash]`, `num_trades=0`, and all
  ratios default to 0.0 (not NaN). This matches the current behavior in the "insufficient
  data" branch at L758.
- **All-zero predictions**: z-score is 0.0 on every step, every built-in strategy returns
  HOLD, `portfolio_curve` is flat at `initial_cash`, `sharpe=0`, `max_drawdown_pct=0`,
  `num_trades=0`. Not an error.
- **Price gaps (missing dates in input frame)**: engine **raises** `ValueError("non-monotonic
  or gapped index")`. We do not forward-fill or skip silently. `engineer_features_v3`
  already drops NaN rows upstream; the engine receives a dense frame or it fails loudly.
  Rationale: silent forward-fill would desync walk-forward fairness.
- **Samples < min_zscore_samples**: engine sets `z_score=0.0` and `samples_ready=False`.
  Built-in strategies return HOLD. No BUY/SELL can fire. Test asserts
  `signals[:min_zscore_samples] == ['HOLD'] * min_zscore_samples` for a run seeded from
  empty.

---

## 8. How callers become thin wrappers

### `predict_ticker` (inline loop, L484-502)

Replaced with one `run()` call. Construction:

```
cfg = BacktestConfig(symbol=symbol, retrain_freq_days=None,   # one-shot
                     ensemble_builder='oof', feature_version=ver)
result = BacktestEngine(cfg, dc).run(full_signal)   # or buy_only per strat
```

The surrounding function still returns the rich dict it currently returns (feature
diagnostics, val-MAE, dir_acc, etc.) — most of those are pre-engine concerns (validation
metrics on the *holdout* before the engine loop). The engine only replaces the inline
mini-backtest (`seed_n=min(20, max(5, len(Xvl)//3))` block at L485-501).

Signature change: **none visible to `api_server.py`**. `predict_ticker(symbol, ...)` keeps
its existing args and return shape. api_server.py:288, 454, 526 continue to work unchanged.

### `backtest_symbol` (L742-814)

Replaced wholesale with:

```
cfg = BacktestConfig(symbol=symbol, retrain_freq_days=RETRAIN_FREQ_V3,
                     ensemble_builder='oof', feature_version=ver)
result = BacktestEngine(cfg, dc).run(full_signal if strat=='full' else buy_only)
```

Signature change: **none**. `backtest_symbol(symbol, cache_dir, version, initial_cash,
strategy)` keeps its signature. api_server.py does not import this function directly.

### `backtest_multi_strategy` (L816-929)

Replaced with ONE `run_multi()` call — the whole point of `run_multi` existing:

```
result = BacktestEngine(cfg, dc).run_multi({
    'full': full_signal,
    'buy_only': buy_only,
})
# Compose the existing dict shape the UI expects
```

Signature change: **none**. api_server.py:735,739 (the ThreadPoolExecutor submits) continue
to call `backtest_multi_strategy(symbol, ...)` unchanged.

**Important**: the current return shape embeds `buyhold` as a separately-computed benchmark
(not strategy-driven). The engine does NOT compute buyhold inside `run_multi` — that is a
presentation concern. The wrapper in `finance_model_v2.py` computes buyhold after the engine
returns, exactly as it does today (L907).

---

## 9. Determinism commitments

- `random_state=42` is threaded explicitly from `BacktestConfig.random_state` into every
  stochastic sklearn estimator inside the builder. Currently it is baked as `random_state=42`
  in the builder bodies (not passed in). Phase 3 lifts it to config.
- **LightGBM thread pin** (wright's concern): `n_jobs=1` is set explicitly on the LightGBM
  estimator inside the builder. This eliminates thread-scheduling non-determinism that
  otherwise causes small last-digit drift on re-runs. Same pin is applied to XGBoost. RF
  already uses `n_jobs=1` in our setup per config.
- **pip freeze snapshot**: `BacktestEngine.run()` captures `sys.version` and a short list of
  versions for `{lightgbm, xgboost, sklearn, numpy, pandas}` into `BacktestResult.metadata`.
  No full `pip freeze` — too noisy and includes unrelated packages. One-line per-library
  version is enough for cross-machine diff debugging.
- **Hash manifest of baseline JSONs** — in scope for Phase 4, NOT for Phase 2. Phase 2
  produces `BacktestResult.config_hash` (sha256 of frozen config fields) as a building
  block, but does not write a manifest file. Phase 4 builds
  `tests/backtest_baselines/MANIFEST.sha256` that pins the 28 JSON hashes + the post-refactor
  hashes (with the three expected MSFT-row diffs called out).

---

## 10. Out of scope this round

- **C-4 retry logic** on `fetch_stock_data`: the engine receives an already-fetched,
  engineered frame. Retry lives one layer up, in the callers. No engine change needed
  when C-4 lands.
- **C-5** (related — data-source flakiness handling): same layer as C-4. Engine is
  downstream. Independent refactor.
- **C-9 deferred** (the "what if we support intraday bars" question): the engine's
  per-timestep loop is agnostic to bar width — dates are strings, prices are floats — but
  features, retrain cadence, and MIN_TRAIN_DAYS constants assume daily. Intraday is a
  separate design. Not this round.

---

## 11. Phase plan recap

- Phase 1 (done): capture 28 baselines. MSFT drift identified, NVDA confirmed-non-drift,
  SHORTHIST_AAPL shows the walk-forward-lost-money-but-predict-inline-didn't signature.
- Phase 2 (this doc): design. Awaits wright review. No code.
- Phase 3: write `backtest_engine.py`. Port all three callers to thin wrappers.
  `finance_model_v2.py` net line change: approximately -150 LOC.
- Phase 4: byte-identical assertion tests. Target: `backtest_symbol` and `predict_ticker`
  outputs match their Phase 1 JSONs exactly. `backtest_multi_strategy` asserts match on
  5/7 tickers (SPY, JNJ, KO, AAPL, NVDA, SHORTHIST_AAPL — that's 6, actually) and
  documents the MSFT delta as a known-good change.
- Phase 5: delete the unchosen ensemble builder; clean up dead code in `finance_model_v2.py`.

End of design.
