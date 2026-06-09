"""
Profile + per-algorithm mode tests.

Covers:
  * Mode enum surfaces on params.
  * Two CopyTradeAlgorithm instances with distinct names + modes coexist.
  * COPYTRADE_MODE env var picks the default mode.
  * Legacy PAPER_TRADE env var still chooses the default when COPYTRADE_MODE
    is unset (backward compat bridge).
"""

import importlib
import os

import pytest

from algorithms.copy_trade import CopyTradeAlgorithm, CopyTradeParams
from bot.algorithm import Mode


def test_default_mode_is_paper(monkeypatch):
    """Fail-safe default: no env vars → PAPER."""
    monkeypatch.delenv("COPYTRADE_MODE", raising=False)
    monkeypatch.delenv("PAPER_TRADE", raising=False)
    # Re-import to re-evaluate the default_factory.
    from algorithms.copy_trade import params as params_mod
    importlib.reload(params_mod)
    assert params_mod.CopyTradeParams().mode == Mode.PAPER


def test_explicit_copytrade_mode_env(monkeypatch):
    monkeypatch.setenv("COPYTRADE_MODE", "live")
    monkeypatch.delenv("PAPER_TRADE", raising=False)
    from algorithms.copy_trade import params as params_mod
    importlib.reload(params_mod)
    assert params_mod.CopyTradeParams().mode == Mode.LIVE


def test_legacy_paper_trade_env_bridge(monkeypatch):
    """PAPER_TRADE=false (legacy) → LIVE when COPYTRADE_MODE unset."""
    monkeypatch.delenv("COPYTRADE_MODE", raising=False)
    monkeypatch.setenv("PAPER_TRADE", "false")
    from algorithms.copy_trade import params as params_mod
    importlib.reload(params_mod)
    assert params_mod.CopyTradeParams().mode == Mode.LIVE


def test_copytrade_mode_wins_over_legacy(monkeypatch):
    """Explicit COPYTRADE_MODE takes precedence over legacy PAPER_TRADE."""
    monkeypatch.setenv("COPYTRADE_MODE", "paper")
    monkeypatch.setenv("PAPER_TRADE", "false")    # would otherwise mean LIVE
    from algorithms.copy_trade import params as params_mod
    importlib.reload(params_mod)
    assert params_mod.CopyTradeParams().mode == Mode.PAPER


# ---------------------------------------------------------------------------
# Multiple instances coexist
# ---------------------------------------------------------------------------


def test_two_instances_have_distinct_names_and_modes():
    """The same class, instantiated twice with different params, must keep
    those params separate — proves no leftover class-level mutable state."""
    from dataclasses import replace

    base = CopyTradeParams()
    a = CopyTradeAlgorithm(
        params=replace(base, name="copy_trade_prod", mode=Mode.LIVE),
    )
    b = CopyTradeAlgorithm(
        params=replace(base, name="copy_trade_paper", mode=Mode.PAPER),
    )

    assert a.params.name == "copy_trade_prod"
    assert a.params.mode == Mode.LIVE
    assert b.params.name == "copy_trade_paper"
    assert b.params.mode == Mode.PAPER

    # Internal caches must also be separate (not shared at class level).
    a.holding_cache.set("m1", 100_000.0)
    assert b.holding_cache.get("m1") is None


def test_name_kwarg_clones_default_params():
    """The shortcut `CopyTradeAlgorithm(name="x")` should rename PARAMS
    without forcing the caller to construct a full params object."""
    a = CopyTradeAlgorithm(name="copy_trade_variant")
    assert a.params.name == "copy_trade_variant"


# ---------------------------------------------------------------------------
# Profile loading via PROFILE env var
# ---------------------------------------------------------------------------


def test_profile_loads_named_module(monkeypatch):
    """Setting PROFILE pulls in algorithms/profiles/<name>.py."""
    monkeypatch.setenv("PROFILE", "experimental")
    import algorithms
    importlib.reload(algorithms)
    # Experimental profile has at least one algorithm, all paper.
    assert len(algorithms.ENABLED) >= 1
    assert all(a.params.mode == Mode.PAPER for a in algorithms.ENABLED)


def test_profile_missing_raises_clear_error(monkeypatch):
    monkeypatch.setenv("PROFILE", "does_not_exist")
    import algorithms
    with pytest.raises(RuntimeError, match="not found"):
        importlib.reload(algorithms)


def test_default_profile_when_unset(monkeypatch):
    monkeypatch.delenv("PROFILE", raising=False)
    import algorithms
    importlib.reload(algorithms)
    assert len(algorithms.ENABLED) >= 1
