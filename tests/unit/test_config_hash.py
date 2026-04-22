"""Config-hash regression tests.

Wright note #4: ``config_hash`` must flip when ``random_state`` changes
(because changing the seed IS supposed to change the backtest). The hash
covers all fields; two configs identical except for random_state must have
different hashes, and two configs identical in every field must have the
same hash.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backtest_engine import BacktestConfig


def test_hash_flips_on_random_state_change() -> None:
    cfg_a = BacktestConfig(symbol="X", random_state=42)
    cfg_b = BacktestConfig(symbol="X", random_state=43)
    ha, hb = cfg_a.hash(), cfg_b.hash()
    assert ha != hb, f"hash should differ on random_state change; got {ha} == {hb}"
    print(f"  [hash] random_state flip OK: 42->{ha[:8]} vs 43->{hb[:8]}")


def test_hash_stable_for_identical_config() -> None:
    cfg_a = BacktestConfig(symbol="X", random_state=42)
    cfg_b = BacktestConfig(symbol="X", random_state=42)
    assert cfg_a.hash() == cfg_b.hash(), "identical configs must hash identically"
    print(f"  [hash] identical configs hash equally OK: {cfg_a.hash()[:16]}")


def test_hash_flips_on_symbol_change() -> None:
    cfg_a = BacktestConfig(symbol="AAPL")
    cfg_b = BacktestConfig(symbol="MSFT")
    assert cfg_a.hash() != cfg_b.hash(), "hash should differ on symbol change"
    print(f"  [hash] symbol flip OK")


def test_hash_flips_on_ensemble_builder_change() -> None:
    cfg_a = BacktestConfig(symbol="X", ensemble_builder="oof")
    cfg_b = BacktestConfig(symbol="X", ensemble_builder="fast")
    assert cfg_a.hash() != cfg_b.hash(), "hash should differ on ensemble_builder change"
    print(f"  [hash] ensemble_builder flip OK")


def test_hash_flips_on_threshold_change() -> None:
    cfg_a = BacktestConfig(symbol="X", threshold=2.5)
    cfg_b = BacktestConfig(symbol="X", threshold=2.0)
    assert cfg_a.hash() != cfg_b.hash(), "hash should differ on threshold change"
    print(f"  [hash] threshold flip OK")


def test_hash_is_hex_digest() -> None:
    h = BacktestConfig(symbol="X").hash()
    assert len(h) == 64, f"expected 64-char sha256 hex, got {len(h)}: {h}"
    assert all(c in "0123456789abcdef" for c in h), "expected all hex chars"
    print(f"  [hash] hex digest shape OK: {h[:16]}...")


def run_all() -> int:
    failed = 0
    for test in (
        test_hash_flips_on_random_state_change,
        test_hash_stable_for_identical_config,
        test_hash_flips_on_symbol_change,
        test_hash_flips_on_ensemble_builder_change,
        test_hash_flips_on_threshold_change,
        test_hash_is_hex_digest,
    ):
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"  FAIL  {test.__name__}: {exc}")
            failed += 1
    return failed


if __name__ == "__main__":
    print("test_config_hash")
    rc = run_all()
    sys.exit(1 if rc else 0)
