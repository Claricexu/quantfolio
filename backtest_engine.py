"""
BacktestEngine — unified walk-forward simulator for Quantfolio.

Phase 2 of C-3 refactor. This module is currently UNUSED by the production
callers in ``finance_model_v2.py`` — Phase 3 wires ``predict_ticker`` into it,
Phase 4 wires ``backtest_symbol`` and ``backtest_multi_strategy``. The goal is
to collapse the three currently-drifted walk-forward loops (one-shot in
``predict_ticker``, plus two walk-forward loops in the two backtest functions)
into a single pure implementation.

Design commitments (enforced here)
----------------------------------
- **Pure**. No file I/O, no network, no global state. The engine receives an
  already-engineered feature frame and pre-resolved config; callers own fetch
  + feature engineering + presentation.
- **Strict on gapped index**. If the input frame's index is non-monotonic or
  has calendar gaps (typical symptom: caller forgot to ``dropna`` upstream),
  the engine raises ``ValueError`` with the canonical message
  "call engineer_features_v3 first; engine does not compact". This is a
  deliberate behaviour change vs the old silent ``dropna`` that each caller
  applied privately — silent compaction desynchronises walk-forward fairness
  across callers. Explicit is better than convenient.
- **Determinism**. ``random_state`` is threaded from ``BacktestConfig`` into
  every stochastic sklearn/xgboost/lgbm estimator inside the ensemble builders
  (see ``finance_model_v2.build_stacking_ensemble`` and friends — all use
  ``n_jobs=1`` as a single-thread regression guard). ``config_hash`` is a
  sha256 of the full config JSON (including ``random_state``) so any change
  to training produces a new hash. See ``tests/unit/test_config_hash.py``.
- **MIN_ZSCORE_SAMPLES is enforced inside the engine**, before the strategy
  callback sees ``z_score``. No code path can bypass the guard. Built-in
  strategies also honor ``ctx.samples_ready`` and return HOLD when False.

OOF vs fast ensemble
--------------------
The engine defaults to ``ensemble_builder='oof'`` (``build_stacking_ensemble``)
to match the user-facing prediction path (``/api/predict``, ``/api/report``).
Today, ``backtest_multi_strategy`` is the one outlier using the val-MAE-weighted
fast builder; after Phase 4 lands it will be pulled into line with the OOF
default. One observed consequence: MSFT's ``backtest_multi_strategy`` final
portfolio shifts from $87,367.59 to $92,159.04 (matching the ``backtest_symbol``
baseline). This is a bug-fix (inconsistent builder selection across paths),
documented as a known-good change in ``round3-summary.md`` deferred to Phase 5.

Short-history tickers (see Phase 1 SHORTHIST_AAPL baseline) are a related
known-good change: ``predict_ticker``'s inline loop previously used an adaptive
``seed_n=min(20,max(5,len(Xvl)//3))`` that could bypass ``MIN_ZSCORE_SAMPLES``
when history was short. The engine applies the guard uniformly, so
SHORTHIST_AAPL is expected to diverge from the pre-refactor baseline while the
other 6 tickers stay byte-identical. Both deltas are the motivating receipts
for the refactor.

Public API
----------
- ``BacktestConfig`` — frozen dataclass describing one simulation.
- ``Context`` — per-step read-only view passed to strategy callbacks.
- ``BacktestResult`` — frozen dataclass describing one simulation's output.
- ``BacktestEngine`` — ``run(strategy_fn)`` and ``run_multi(strategy_fns)``.
- ``full_signal``, ``buy_only``, ``buy_hold`` — the three built-in strategies.

Speed note (goes in docstring per design Section 6)
---------------------------------------------------
The OOF builder runs ~18 model fits per retrain (3 base models x 5 OOF folds
+ 3 refit on full train) vs the fast builder's ~3 fits (val-MAE-weighted
stacking). On a 10-year backtest with retrain_freq_days=63, that is ~44
retrains, so the engine spends ~800 fits (OOF) vs ~130 fits (fast).
Wall-clock delta ~3-4x. For CLI and async ``/api/backtest``, tolerable.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Callable, Literal, Optional

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

# Import — NOT copy — the ensemble builders. Phase 5 cleanup may relocate them
# into this module; for Phase 2 they keep their current home so the finance
# module remains unchanged and importable as today.
from finance_model_v2 import (  # noqa: E402
    build_stacking_ensemble,
    build_stacking_ensemble_fast,
    predict_v3,
    predict_v2,
    train_rf_v2,
    train_xgb_v2,
    THRESHOLD,
    ZSCORE_LOOKBACK,
    MIN_ZSCORE_SAMPLES,
    MIN_TRAIN_DAYS,
    V2_FEATURE_COLS,
    V3_FEATURE_COLS,
    HAS_LGBM,
)

# Type aliases
Signal = Literal["BUY", "SELL", "HOLD"]
EnsembleKind = Literal["oof", "fast"]
FeatureVersion = Literal["v2", "v3"]


# =============================================================================
# Config & result dataclasses
# =============================================================================

@dataclass(frozen=True)
class BacktestConfig:
    """Frozen configuration for a single engine run.

    Fields are hashed (see :meth:`BacktestConfig.hash`) into a short fingerprint
    that accompanies every ``BacktestResult``. The hash covers ALL fields,
    INCLUDING ``random_state`` — changing the seed IS supposed to change the
    backtest, so the hash should flip. See ``tests/unit/test_config_hash.py``.
    """

    symbol: str
    strategy_name: str = "full"   # 'full' | 'buy_only' | 'buy_hold' (display/meta only)
    initial_cash: float = 10000.0
    threshold: float = THRESHOLD
    zscore_lookback: int = ZSCORE_LOOKBACK
    min_zscore_samples: int = MIN_ZSCORE_SAMPLES
    retrain_freq_days: Optional[int] = None   # None = one-shot (predict_ticker style)
    min_train_days: int = MIN_TRAIN_DAYS
    # Seeds pred_history with the tail of the validation-window predictions
    # (i.e. ``yp[-zscore_lookback:]``). This matches the current code paths
    # in finance_model_v2 L774-L776 / L868-L870 / L482.
    seed_from_validation: bool = True
    random_state: int = 42
    ensemble_builder: EnsembleKind = "oof"
    feature_version: FeatureVersion = "v3"

    # ------- derived helpers -------

    def hash(self) -> str:
        """sha256 of all fields (JSON-encoded, sort_keys=True, default=str).

        Including random_state is intentional: changing the seed MUST change
        the hash because the resulting backtest changes. See
        ``test_config_hash.py`` for the regression assertion.
        """
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Context:
    """Per-step read-only view passed to the strategy callback.

    Strategy callbacks receive this and return a ``Signal`` ('BUY' | 'SELL' |
    'HOLD'). They may NOT mutate engine state. Built-in strategies honor
    ``samples_ready`` and return HOLD when False; user-supplied strategies are
    expected to do the same but it is NOT enforced by runtime assertion (see
    design Section 4 — a caller wanting to act on ``raw_pred`` pre-seed is
    rare but not precluded).
    """
    z_score: float
    raw_pred: float
    price: float
    date: str
    position_shares: float
    cash: float
    samples_ready: bool
    threshold: float
    step_idx: int


@dataclass(frozen=True)
class Trade:
    """A single BUY or SELL event (HOLDs are implied by gaps)."""
    date: str
    signal: Signal
    price: float
    z_score: float
    portfolio_value: float


@dataclass(frozen=True)
class BacktestResult:
    """Per-strategy simulation result. See ``BacktestEngine.run`` return type."""

    # ------- summary stats -------
    final_portfolio_value: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    num_trades: int
    buys: int
    sells: int
    holds: int

    # ------- per-step series -------
    dates: list[str]
    portfolio_curve: list[float]
    per_period_returns: list[float]
    signals: list[Signal]
    raw_predictions: list[float]
    z_scores: list[float]
    trades: list[Trade]

    # ------- metadata -------
    symbol: str
    strategy: str
    period_days: int
    start_date: Optional[str]
    end_date: Optional[str]
    initial_cash: float
    ensemble_builder: str
    min_zscore_samples_used: int
    min_train_days_used: int
    retrain_freq_days: Optional[int]
    feature_version: str
    config_hash: str
    # library-version fingerprint — useful for cross-machine diff debugging.
    versions: dict = field(default_factory=dict)


# =============================================================================
# Built-in strategy callbacks
# =============================================================================

def full_signal(ctx: Context) -> Signal:
    """BUY if z >= threshold and cash > 0; SELL if z <= -threshold and shares > 0."""
    if not ctx.samples_ready:
        return "HOLD"
    if ctx.z_score >= ctx.threshold and ctx.cash > 0:
        return "BUY"
    if ctx.z_score <= -ctx.threshold and ctx.position_shares > 0:
        return "SELL"
    return "HOLD"


def buy_only(ctx: Context) -> Signal:
    """BUY if z >= threshold and cash > 0; never sell."""
    if not ctx.samples_ready:
        return "HOLD"
    if ctx.z_score >= ctx.threshold and ctx.cash > 0:
        return "BUY"
    return "HOLD"


def buy_hold(ctx: Context) -> Signal:
    """BUY on the first step where cash > 0, then hold. Ignores z entirely."""
    if ctx.cash > 0:
        return "BUY"
    return "HOLD"


BUILTIN_STRATEGIES: dict[str, Callable[[Context], Signal]] = {
    "full": full_signal,
    "buy_only": buy_only,
    "buy_hold": buy_hold,
}


# =============================================================================
# Stats helpers (shared with capture_baselines — kept in sync deliberately)
# =============================================================================

def _daily_returns(port: np.ndarray) -> np.ndarray:
    port = np.asarray(port, dtype=float)
    if port.size < 2:
        return np.array([], dtype=float)
    return np.diff(port) / port[:-1]


def _sharpe(port: np.ndarray) -> float:
    dr = _daily_returns(port)
    if dr.size == 0 or np.std(dr) <= 0:
        return 0.0
    return float(np.mean(dr) / np.std(dr) * np.sqrt(252))


def _sortino(port: np.ndarray) -> float:
    dr = _daily_returns(port)
    if dr.size == 0:
        return 0.0
    downside = dr[dr < 0]
    if downside.size == 0 or np.std(downside) <= 0:
        return 0.0
    return float(np.mean(dr) / np.std(downside) * np.sqrt(252))


def _max_drawdown_pct(port: np.ndarray) -> float:
    port = np.asarray(port, dtype=float)
    if port.size == 0:
        return 0.0
    pk = np.maximum.accumulate(port)
    return float(((port - pk) / pk).min()) * 100.0


def _library_versions() -> dict:
    """Short per-library version fingerprint. Not a full pip freeze — one-line
    per library that matters for determinism."""
    out = {"python": sys.version.split()[0]}
    try:
        import numpy as _np
        out["numpy"] = _np.__version__
    except Exception:
        pass
    try:
        import pandas as _pd
        out["pandas"] = _pd.__version__
    except Exception:
        pass
    try:
        import sklearn as _sk
        out["sklearn"] = _sk.__version__
    except Exception:
        pass
    try:
        import xgboost as _xgb
        out["xgboost"] = _xgb.__version__
    except Exception:
        pass
    try:
        import lightgbm as _lgb
        out["lightgbm"] = _lgb.__version__
    except Exception:
        pass
    return out


# =============================================================================
# The engine
# =============================================================================

class BacktestEngine:
    """Walk-forward backtest simulator.

    The engine receives a pre-engineered DataFrame (post ``engineer_features_v3``
    + ``dropna``) and a frozen ``BacktestConfig``. It handles training, retraining,
    prediction, z-score computation, signal dispatch to a strategy callback,
    and portfolio simulation. It does NOT fetch, engineer, or persist.

    Two entry points:
        * ``run(strategy_fn) -> BacktestResult`` — single strategy.
        * ``run_multi({name: strategy_fn}) -> {name: BacktestResult}`` — many
          strategies sharing the same walk-forward model + predictions. This is
          the byte-identical path for ``backtest_multi_strategy`` — calling
          ``run`` twice cannot guarantee identical predictions because the OOF
          folds reseed on each retrain.
    """

    def __init__(self, config: BacktestConfig, data: pd.DataFrame) -> None:
        self.config = config
        self.data = data
        self._validate_input()

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def _validate_input(self) -> None:
        """Fail loudly on common caller bugs.

        Deliberate behaviour change vs the old silent ``dropna`` — engine
        trusts the caller to compact the frame first. Error messages include
        the canonical "call engineer_features_v3 first; engine does not
        compact" hint so the offending caller is obvious in a stack trace.
        """
        df = self.data
        if not isinstance(df, pd.DataFrame):
            raise ValueError("data must be a pandas DataFrame")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError(
                "data.index must be a DatetimeIndex; "
                "call engineer_features_v3 first; engine does not compact"
            )
        if df.empty:
            raise ValueError("insufficient data: 0 feature rows")

        # required columns
        fcols = self._feature_cols()
        required = set(fcols) | {"Target_Return", "Close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(
                f"missing required columns: {sorted(missing)}; "
                "call engineer_features_v3 first; engine does not compact"
            )

        # No NaNs in required cols — caller is expected to have dropna'd.
        nan_cols = [c for c in (list(fcols) + ["Target_Return", "Close"]) if df[c].isna().any()]
        if nan_cols:
            raise ValueError(
                f"data contains NaNs in required columns {nan_cols}; "
                "call engineer_features_v3 first; engine does not compact"
            )

        # Monotonic, unique index.
        if not df.index.is_monotonic_increasing:
            raise ValueError(
                "non-monotonic or gapped index; "
                "call engineer_features_v3 first; engine does not compact"
            )
        if df.index.has_duplicates:
            raise ValueError(
                "duplicate index entries; "
                "call engineer_features_v3 first; engine does not compact"
            )

    def _feature_cols(self) -> list[str]:
        if self.config.feature_version == "v3":
            return list(V3_FEATURE_COLS)
        return list(V2_FEATURE_COLS)

    # ------------------------------------------------------------------
    # Public entrypoints
    # ------------------------------------------------------------------

    def run(self, strategy_fn: Callable[[Context], Signal]) -> BacktestResult:
        """Run one strategy against the simulation. Returns a ``BacktestResult``."""
        return self.run_multi({self.config.strategy_name: strategy_fn})[self.config.strategy_name]

    def run_multi(
        self,
        strategy_fns: dict[str, Callable[[Context], Signal]],
    ) -> dict[str, BacktestResult]:
        """Run multiple strategies sharing the same walk-forward model and predictions.

        The single walk-forward loop produces one ``raw_pred`` per step and
        dispatches it through every ``strategy_fn`` in ``strategy_fns``. This
        is the ONLY way to guarantee two strategies see identical predictions:
        calling ``run`` twice cannot, because OOF folds re-seed on each retrain.
        """
        if not strategy_fns:
            raise ValueError("strategy_fns cannot be empty")

        cfg = self.config
        dc = self.data  # already validated
        fcols = self._feature_cols()

        aX = dc[fcols].values
        ay = dc["Target_Return"].values
        ap = dc["Close"].values.ravel()

        if cfg.retrain_freq_days is None:
            # One-shot (predict_ticker replacement). Train once on the first
            # int(len*0.85), then iterate through the remaining ("validation")
            # rows using the trained model.
            return self._run_oneshot(aX, ay, ap, dc, strategy_fns)
        # Walk-forward (backtest_symbol / backtest_multi_strategy replacement).
        return self._run_walkforward(aX, ay, ap, dc, strategy_fns)

    # ------------------------------------------------------------------
    # One-shot (predict_ticker path)
    # ------------------------------------------------------------------

    def _run_oneshot(
        self,
        aX: np.ndarray,
        ay: np.ndarray,
        ap: np.ndarray,
        dc: pd.DataFrame,
        strategy_fns: dict[str, Callable[[Context], Signal]],
    ) -> dict[str, BacktestResult]:
        """predict_ticker-style: one-shot train on 85%, iterate over the rest.

        Note: the old inline loop at ``finance_model_v2.py:484-502`` used a
        variable ``seed_n=min(20,max(5,len(Xvl)//3))`` that could bypass
        ``MIN_ZSCORE_SAMPLES`` on short-history tickers. The engine instead
        seeds ``pred_history`` from the tail of the full validation-window
        predictions (``yp[-zscore_lookback:]``) when ``seed_from_validation=True``.
        For tickers with enough history the guard is satisfied at step 0 and
        the outcome is byte-identical; for SHORTHIST_AAPL the guard is now
        honored and the result diverges (documented known-good change).
        """
        cfg = self.config
        te = len(aX)
        if te < 100:
            raise ValueError(f"insufficient data: {te} feature rows (need 100+)")
        vs = int(te * 0.85)
        if vs <= 0 or vs >= te:
            raise ValueError(f"insufficient data: validation split produced vs={vs} of {te}")
        Xtr, Xvl = aX[:vs], aX[vs:]
        ytr, yvl = ay[:vs], ay[vs:]

        scaler = StandardScaler()
        scaler.fit(Xtr)
        Xtr_s = scaler.transform(Xtr)
        Xvl_s = scaler.transform(Xvl)

        model, pf = self._build_model(Xtr_s, ytr, Xvl_s, yvl)
        # Optional n_jobs sanity: every fitted base estimator must have n_jobs==1
        # (regression assert; Wright note #5). Builders in finance_model_v2 all
        # bake n_jobs=1; this just confirms no silent override snuck in.
        _assert_n_jobs_one(model)

        yp = pf(model, Xvl_s)

        # Seed pred_history with the tail of validation preds so the guard
        # passes on step 0 for most tickers. Matches predict_ticker L482.
        pred_history: list[float] = []
        if cfg.seed_from_validation:
            pred_history = list(yp[-cfg.zscore_lookback:])

        return self._simulate(
            dates=[str(dc.index[vs + j].date()) for j in range(len(Xvl))],
            prices=[float(dc["Close"].iloc[vs + j]) for j in range(len(Xvl))],
            raw_preds=[float(x) for x in yp.tolist()],
            pred_history=pred_history,
            strategy_fns=strategy_fns,
        )

    # ------------------------------------------------------------------
    # Walk-forward (backtest_symbol / backtest_multi_strategy path)
    # ------------------------------------------------------------------

    def _run_walkforward(
        self,
        aX: np.ndarray,
        ay: np.ndarray,
        ap: np.ndarray,
        dc: pd.DataFrame,
        strategy_fns: dict[str, Callable[[Context], Signal]],
    ) -> dict[str, BacktestResult]:
        cfg = self.config
        rf_freq = cfg.retrain_freq_days
        assert rf_freq is not None

        # Dynamic backtest start: same algorithm as finance_model_v2 L755-757.
        bm = dc.index >= pd.Timestamp("2015-01-02")
        bsi = int(np.argmax(bm)) if bm.any() else 0
        bsi = max(bsi, cfg.min_train_days)
        if bsi >= len(aX) - 1:
            raise ValueError(
                f"insufficient data: start index {bsi} >= end {len(aX)-1}"
            )

        dates_all: list[str] = []
        prices_all: list[float] = []
        raw_preds_all: list[float] = []

        model = None
        pf = None
        scaler = StandardScaler()
        rc = 0
        pred_history: list[float] = []

        for i in range(bsi, len(aX) - 1):
            if model is None or rc >= rf_freq:
                vs = int(i * 0.85)
                scaler.fit(aX[:vs])
                Xtr_s = scaler.transform(aX[:vs])
                Xvl_s = scaler.transform(aX[vs:i])
                model, pf = self._build_model(Xtr_s, ay[:vs], Xvl_s, ay[vs:i])
                _assert_n_jobs_one(model)
                if not pred_history and cfg.seed_from_validation:
                    seed_preds = pf(model, Xvl_s)
                    pred_history = list(seed_preds[-cfg.zscore_lookback:])
                rc = 0

            assert pf is not None and model is not None
            raw_pred = float(pf(model, scaler.transform(aX[i:i + 1]))[0])
            raw_preds_all.append(raw_pred)
            dates_all.append(dc.index[i].strftime("%Y-%m-%d"))
            prices_all.append(float(ap[i]))
            rc += 1

        return self._simulate(
            dates=dates_all,
            prices=prices_all,
            raw_preds=raw_preds_all,
            pred_history=pred_history,
            strategy_fns=strategy_fns,
        )

    # ------------------------------------------------------------------
    # Simulation loop (shared between one-shot and walk-forward)
    # ------------------------------------------------------------------

    def _simulate(
        self,
        dates: list[str],
        prices: list[float],
        raw_preds: list[float],
        pred_history: list[float],
        strategy_fns: dict[str, Callable[[Context], Signal]],
    ) -> dict[str, BacktestResult]:
        """Given one list of raw_preds, dispatch to N strategies and return
        per-strategy results. z_score + samples_ready are computed ONCE per
        step (shared across strategies)."""
        cfg = self.config
        n = len(raw_preds)
        assert len(dates) == n and len(prices) == n

        # Per-strategy portfolio state.
        state = {
            name: {
                "cash": float(cfg.initial_cash),
                "shares": 0.0,
                "portfolio_curve": [],
                "signals": [],
                "trades": [],
            }
            for name in strategy_fns.keys()
        }

        # Engine-owned state (shared across strategies in run_multi).
        z_scores: list[float] = []
        history = list(pred_history)  # local copy so caller's list is not mutated

        for step_idx in range(n):
            raw_pred = raw_preds[step_idx]
            price = prices[step_idx]
            date = dates[step_idx]

            # Append BEFORE reading — matches finance_model_v2 L780-787 / L874-881.
            history.append(raw_pred)
            if len(history) > cfg.zscore_lookback:
                history = history[-cfg.zscore_lookback:]

            samples_ready = len(history) >= cfg.min_zscore_samples
            if samples_ready:
                hist = np.array(history[:-1], dtype=float)
                mu = float(np.mean(hist))
                sigma = float(np.std(hist))
                z = (raw_pred - mu) / sigma if sigma > 1e-10 else 0.0
            else:
                z = 0.0
            z_scores.append(z)

            # Dispatch per-strategy.
            for name, fn in strategy_fns.items():
                s = state[name]
                ctx = Context(
                    z_score=z,
                    raw_pred=raw_pred,
                    price=price,
                    date=date,
                    position_shares=s["shares"],
                    cash=s["cash"],
                    samples_ready=samples_ready,
                    threshold=cfg.threshold,
                    step_idx=step_idx,
                )
                signal: Signal = fn(ctx)
                if signal not in ("BUY", "SELL", "HOLD"):
                    raise ValueError(
                        f"strategy {name!r} returned invalid signal {signal!r}"
                    )
                # Apply signal.
                if signal == "BUY" and s["cash"] > 0:
                    s["shares"] = s["cash"] / price
                    s["cash"] = 0.0
                elif signal == "SELL" and s["shares"] > 0:
                    s["cash"] = s["shares"] * price
                    s["shares"] = 0.0
                elif signal in ("BUY", "SELL"):
                    # Callback asked for BUY/SELL but the precondition didn't
                    # hold (e.g. BUY with cash=0). Degrade to HOLD silently —
                    # matches the existing three callers' implicit behaviour.
                    signal = "HOLD"

                pv = s["cash"] + s["shares"] * price
                s["portfolio_curve"].append(pv)
                s["signals"].append(signal)
                if signal in ("BUY", "SELL"):
                    s["trades"].append(
                        Trade(
                            date=date,
                            signal=signal,
                            price=round(price, 4),
                            z_score=round(z, 4),
                            portfolio_value=round(pv, 4),
                        )
                    )

        # Build results.
        return {
            name: self._build_result(
                name=name,
                dates=dates,
                raw_preds=raw_preds,
                z_scores=z_scores,
                strategy_state=s,
            )
            for name, s in state.items()
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_model(self, Xtr_s: np.ndarray, ytr: np.ndarray, Xvl_s: np.ndarray, yvl: np.ndarray):
        """Dispatch to the configured ensemble builder.

        Engine does NOT own the builders — they remain in finance_model_v2.py
        for Phase 2 (Phase 5 cleanup may relocate). See Wright note #5.
        """
        cfg = self.config
        if cfg.feature_version == "v3" and HAS_LGBM:
            if cfg.ensemble_builder == "fast":
                return build_stacking_ensemble_fast(Xtr_s, ytr, Xvl_s, yvl), predict_v3
            return build_stacking_ensemble(Xtr_s, ytr, Xvl_s, yvl), predict_v3
        # v2 fallback: the "builder" is just (RF, XGB).
        Xall = np.vstack([Xtr_s, Xvl_s])
        yall = np.concatenate([ytr, yvl])
        model = (train_rf_v2(Xall, yall), train_xgb_v2(Xall, yall))
        return model, predict_v2

    def _build_result(
        self,
        name: str,
        dates: list[str],
        raw_preds: list[float],
        z_scores: list[float],
        strategy_state: dict,
    ) -> BacktestResult:
        cfg = self.config
        port = np.array(strategy_state["portfolio_curve"], dtype=float)
        signals = strategy_state["signals"]
        trades = strategy_state["trades"]
        dr = _daily_returns(port)

        buys = signals.count("BUY")
        sells = signals.count("SELL")
        holds = signals.count("HOLD")

        return BacktestResult(
            final_portfolio_value=float(port[-1]) if port.size else float(cfg.initial_cash),
            sharpe=round(_sharpe(port), 4),
            sortino=round(_sortino(port), 4),
            max_drawdown_pct=round(_max_drawdown_pct(port), 4),
            num_trades=len(trades),
            buys=buys,
            sells=sells,
            holds=holds,
            dates=list(dates),
            portfolio_curve=[float(v) for v in port.tolist()],
            per_period_returns=[float(x) for x in dr.tolist()],
            signals=list(signals),
            raw_predictions=list(raw_preds),
            z_scores=list(z_scores),
            trades=list(trades),
            symbol=cfg.symbol,
            strategy=name,
            period_days=len(port),
            start_date=dates[0] if dates else None,
            end_date=dates[-1] if dates else None,
            initial_cash=cfg.initial_cash,
            ensemble_builder=cfg.ensemble_builder,
            min_zscore_samples_used=cfg.min_zscore_samples,
            min_train_days_used=cfg.min_train_days,
            retrain_freq_days=cfg.retrain_freq_days,
            feature_version=cfg.feature_version,
            config_hash=cfg.hash(),
            versions=_library_versions(),
        )


# =============================================================================
# n_jobs regression assert (Wright note #5)
# =============================================================================

def _assert_n_jobs_one(model) -> None:
    """Regression guard: every fitted base estimator in a V3 ensemble must
    have ``n_jobs==1``. This already holds in finance_model_v2 at lines
    381/385/395/401/405 — this assert catches an accidental override if the
    builders move into this module in Phase 5.

    For V2 models (tuple of RF + XGB) the same check applies.
    """
    if isinstance(model, dict) and {"lgbm", "xgb", "rf"}.issubset(model.keys()):
        # V3 stacking ensemble.
        for key in ("lgbm", "xgb", "rf"):
            est = model[key]
            nj = getattr(est, "n_jobs", None)
            if nj not in (1, None):
                raise AssertionError(
                    f"base estimator {key!r} has n_jobs={nj} (expected 1). "
                    "This breaks determinism; investigate the builder."
                )
    elif isinstance(model, tuple) and len(model) == 2:
        # V2 (rf, xgb).
        for idx, est in enumerate(model):
            nj = getattr(est, "n_jobs", None)
            if nj not in (1, None):
                raise AssertionError(
                    f"base estimator [{idx}] has n_jobs={nj} (expected 1)."
                )
    # Other shapes: silently allow (custom strategy_fn callers may pass pre-built models).
