"""Unit tests for ``api_server._classify_alert`` — Round 8b refined alert rules.

Plain-assert style, matching the rest of ``tests/unit/``. The function under
test is pure over (lite_sig, pro_sig, best_key); no DB / network / timezone
dependency.

Round 8b rules in plain English (locked decision):
  BUY fires when ANY of:
    (a) Both Lite and Pro signaled BUY (consensus).
    (b) Only Pro signaled BUY (Lite=HOLD) AND best ∈ {pro_buyonly, pro_full}.
    (c) Only Lite signaled BUY (Pro=HOLD) AND best ∈ {lite_buyonly, lite_full}.
  SELL fires after the hard gate clears:
    Hard gate suppresses unconditionally when best ∈
      {buyhold, lite_buyonly, pro_buyonly} (those strategies never SELL in
      backtest, so SELL on the live tape is noise).
    Path A — best == pro_full AND Pro=SELL.
    Path B — best == lite_full AND Lite=SELL.
  Conflict (Lite/Pro on opposite sides BUY vs SELL) → never fires.
  best_strategy null/missing → only consensus BUY (a) can fire.

The 18 cases below cover every named path and every edge case the spec
called out. Adding a 19th path here is a code-smell — extend
_classify_alert in api_server.py first, then add the test.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from api_server import _classify_alert  # noqa: E402


def _run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"  FAIL  {name}: {exc}")
        return 1
    print(f"  PASS  {name}")
    return 0


# ─── BUY paths ─────────────────────────────────────────────────────────────

# 1. Path (a): both models agree on BUY → fires regardless of best_strategy.
def test_buy_consensus_fires_with_any_best_strategy():
    verdict, label = _classify_alert('BUY', 'BUY', 'pro_full')
    assert verdict == 'BUY', f"expected BUY, got {verdict}"
    assert 'consensus' in label, f"expected consensus path, got {label}"


# 2. Path (b): only Pro BUYs + best=pro_buyonly → fires.
def test_buy_pro_only_with_pro_buyonly_best_fires():
    verdict, label = _classify_alert('HOLD', 'BUY', 'pro_buyonly')
    assert verdict == 'BUY'
    assert 'pro-only' in label and 'pro_buyonly' in label


# 3. Path (b): only Pro BUYs + best=pro_full → fires.
def test_buy_pro_only_with_pro_full_best_fires():
    verdict, label = _classify_alert('HOLD', 'BUY', 'pro_full')
    assert verdict == 'BUY'
    assert 'pro-only' in label and 'pro_full' in label


# 4. Path (b): Pro BUYs but best=lite_buyonly (validates Lite, not Pro) → suppress.
def test_buy_pro_only_with_lite_buyonly_best_does_not_fire():
    verdict, _ = _classify_alert('HOLD', 'BUY', 'lite_buyonly')
    assert verdict is None, f"expected None, got {verdict}"


# 5. Path (b): Pro BUYs but best_strategy missing → suppress.
def test_buy_pro_only_with_null_best_does_not_fire():
    verdict, reason = _classify_alert('HOLD', 'BUY', None)
    assert verdict is None
    assert 'null' in reason or 'missing' in reason


# 6. Path (c): only Lite BUYs + best=lite_buyonly → fires.
def test_buy_lite_only_with_lite_buyonly_best_fires():
    verdict, label = _classify_alert('BUY', 'HOLD', 'lite_buyonly')
    assert verdict == 'BUY'
    assert 'lite-only' in label and 'lite_buyonly' in label


# 7. Path (c): only Lite BUYs + best=lite_full → fires.
def test_buy_lite_only_with_lite_full_best_fires():
    verdict, label = _classify_alert('BUY', 'HOLD', 'lite_full')
    assert verdict == 'BUY'
    assert 'lite-only' in label and 'lite_full' in label


# 8. Path (c): Lite BUYs but best=pro_full (validates Pro, not Lite) → suppress.
def test_buy_lite_only_with_pro_full_best_does_not_fire():
    verdict, _ = _classify_alert('BUY', 'HOLD', 'pro_full')
    assert verdict is None


# ─── SELL hard-gate cases ──────────────────────────────────────────────────

# 9. Hard gate: best=buyhold suppresses regardless of model signals.
def test_sell_hard_gate_buyhold_suppresses():
    verdict, reason = _classify_alert('SELL', 'SELL', 'buyhold')
    assert verdict is None
    assert 'gate' in reason.lower() and 'buyhold' in reason


# 10. Hard gate: best=pro_buyonly suppresses regardless of model signals.
def test_sell_hard_gate_pro_buyonly_suppresses():
    verdict, _ = _classify_alert('SELL', 'SELL', 'pro_buyonly')
    assert verdict is None


# 11. Hard gate: best=lite_buyonly suppresses regardless of model signals.
def test_sell_hard_gate_lite_buyonly_suppresses():
    verdict, _ = _classify_alert('SELL', 'SELL', 'lite_buyonly')
    assert verdict is None


# ─── SELL fire/no-fire after gate ──────────────────────────────────────────

# 12. SELL Path A: best=pro_full + Pro=SELL → fires (Lite=HOLD is allowed).
def test_sell_pro_full_with_pro_sell_fires():
    verdict, label = _classify_alert('HOLD', 'SELL', 'pro_full')
    assert verdict == 'SELL'
    assert 'pro-full-signal' in label


# 13. SELL Path A: best=pro_full + Pro neutral → suppress.
def test_sell_pro_full_with_pro_hold_does_not_fire():
    verdict, _ = _classify_alert('SELL', 'HOLD', 'pro_full')
    assert verdict is None, "Pro must be SELL on the pro_full path"


# 14. SELL Path B: best=lite_full + Lite=SELL → fires (Pro=HOLD is allowed).
def test_sell_lite_full_with_lite_sell_fires():
    verdict, label = _classify_alert('SELL', 'HOLD', 'lite_full')
    assert verdict == 'SELL'
    assert 'lite-full-signal' in label


# 15. SELL Path B: best=lite_full + Lite neutral → suppress.
def test_sell_lite_full_with_lite_hold_does_not_fire():
    verdict, _ = _classify_alert('HOLD', 'SELL', 'lite_full')
    assert verdict is None, "Lite must be SELL on the lite_full path"


# ─── Conflict cases ────────────────────────────────────────────────────────

# 16. Conflict: Lite=BUY, Pro=SELL → never fires, even when best=pro_full
#     (would otherwise satisfy SELL Path A).
def test_conflict_lite_buy_pro_sell_does_not_fire():
    verdict, reason = _classify_alert('BUY', 'SELL', 'pro_full')
    assert verdict is None
    assert 'conflict' in reason.lower()


# 17. Conflict: Pro=BUY, Lite=SELL → never fires, even when best=lite_full.
def test_conflict_pro_buy_lite_sell_does_not_fire():
    verdict, reason = _classify_alert('SELL', 'BUY', 'lite_full')
    assert verdict is None
    assert 'conflict' in reason.lower()


# ─── Null best_strategy ────────────────────────────────────────────────────

# 18. Null best_strategy: only consensus BUY can fire; single-model BUY,
#     all SELLs must suppress with the "null/missing" reason for clarity.
def test_null_best_strategy_only_consensus_buy_fires():
    # consensus BUY fires
    v, _ = _classify_alert('BUY', 'BUY', None)
    assert v == 'BUY'
    # single-model Pro BUY suppressed
    v, r = _classify_alert('HOLD', 'BUY', None)
    assert v is None and ('null' in r or 'missing' in r)
    # single-model Lite BUY suppressed
    v, r = _classify_alert('BUY', 'HOLD', None)
    assert v is None and ('null' in r or 'missing' in r)
    # consensus SELL must NOT fire — null best can't validate the SELL path
    v, _ = _classify_alert('SELL', 'SELL', None)
    assert v is None
    # single-model SELLs must NOT fire either
    v, _ = _classify_alert('HOLD', 'SELL', None)
    assert v is None
    v, _ = _classify_alert('SELL', 'HOLD', None)
    assert v is None


# ─── Bonus: HOLD+HOLD → "no model signals" suppression reason ──────────────

def test_no_model_signals_suppression_reason():
    """Soft-coverage check that the no-signal log path is reachable. Not
    counted as one of the 18 spec'd cases — there to keep the suppression
    reason string from rotting into something useless for log-grep."""
    v, r = _classify_alert('HOLD', 'HOLD', 'pro_full')
    assert v is None
    assert 'no model signals' in r.lower()


# ─── Runner ────────────────────────────────────────────────────────────────


def run_all():
    fails = 0
    for name, fn in (
        ("test_buy_consensus_fires_with_any_best_strategy",
         test_buy_consensus_fires_with_any_best_strategy),
        ("test_buy_pro_only_with_pro_buyonly_best_fires",
         test_buy_pro_only_with_pro_buyonly_best_fires),
        ("test_buy_pro_only_with_pro_full_best_fires",
         test_buy_pro_only_with_pro_full_best_fires),
        ("test_buy_pro_only_with_lite_buyonly_best_does_not_fire",
         test_buy_pro_only_with_lite_buyonly_best_does_not_fire),
        ("test_buy_pro_only_with_null_best_does_not_fire",
         test_buy_pro_only_with_null_best_does_not_fire),
        ("test_buy_lite_only_with_lite_buyonly_best_fires",
         test_buy_lite_only_with_lite_buyonly_best_fires),
        ("test_buy_lite_only_with_lite_full_best_fires",
         test_buy_lite_only_with_lite_full_best_fires),
        ("test_buy_lite_only_with_pro_full_best_does_not_fire",
         test_buy_lite_only_with_pro_full_best_does_not_fire),
        ("test_sell_hard_gate_buyhold_suppresses",
         test_sell_hard_gate_buyhold_suppresses),
        ("test_sell_hard_gate_pro_buyonly_suppresses",
         test_sell_hard_gate_pro_buyonly_suppresses),
        ("test_sell_hard_gate_lite_buyonly_suppresses",
         test_sell_hard_gate_lite_buyonly_suppresses),
        ("test_sell_pro_full_with_pro_sell_fires",
         test_sell_pro_full_with_pro_sell_fires),
        ("test_sell_pro_full_with_pro_hold_does_not_fire",
         test_sell_pro_full_with_pro_hold_does_not_fire),
        ("test_sell_lite_full_with_lite_sell_fires",
         test_sell_lite_full_with_lite_sell_fires),
        ("test_sell_lite_full_with_lite_hold_does_not_fire",
         test_sell_lite_full_with_lite_hold_does_not_fire),
        ("test_conflict_lite_buy_pro_sell_does_not_fire",
         test_conflict_lite_buy_pro_sell_does_not_fire),
        ("test_conflict_pro_buy_lite_sell_does_not_fire",
         test_conflict_pro_buy_lite_sell_does_not_fire),
        ("test_null_best_strategy_only_consensus_buy_fires",
         test_null_best_strategy_only_consensus_buy_fires),
        ("test_no_model_signals_suppression_reason",
         test_no_model_signals_suppression_reason),
    ):
        fails += _run(name, fn)
    return fails


if __name__ == "__main__":
    sys.exit(run_all())
