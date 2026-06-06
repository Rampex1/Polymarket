"""
Shared pytest fixtures.

Design rules for this test suite:

  * Use a *real* SQLite database via tempfile — no DB mocking. SQLite is
    fast enough that there's no reason to fake it, and using the real
    engine catches integration bugs (PRAGMA, indexes, migrations) that a
    mock would hide.

  * Use real RiskManager, real PositionTracker, real Trade dataclasses.
    The only thing we stub is the HTTP boundary — we obviously can't hit
    the live Polymarket API, and we want deterministic responses.

  * Each test gets a fresh DB AND a cleared target-holding cache — no
    state leakage between tests.
"""

import os
import sys

import pytest

# Ensure project root is importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db_path(tmp_path):
    """Path to a fresh empty SQLite file. The `fresh_db` fixture wires it in."""
    return str(tmp_path / "test.db")


@pytest.fixture
def fresh_db(tmp_db_path, monkeypatch):
    """Point bot.db at a brand-new file for this test, then close + reset.

    Uses `db.reset_for_tests()` which is the documented teardown hook —
    cleaner than poking the internal `_conn` attribute, and works correctly
    with the new thread-local connection layout.
    """
    from bot import config
    import bot.db as dbmod

    monkeypatch.setattr(config, "DB_PATH", tmp_db_path)
    dbmod.reset_for_tests()
    yield dbmod
    dbmod.reset_for_tests()


@pytest.fixture(autouse=True)
def _clear_holding_cache():
    """Wipe the module-level target-holding cache between tests so prior-test
    state doesn't leak into the next test's sell-ratio computation."""
    from bot import fetcher
    fetcher.target_holding_cache.clear()
    yield
    fetcher.target_holding_cache.clear()


@pytest.fixture
def tracker(fresh_db):
    """A PositionTracker bound to a fresh DB."""
    from bot.positions import PositionTracker

    t = PositionTracker()
    t.init_paper_balance(10_000.0)
    return t


@pytest.fixture
def risk(tracker):
    from bot.positions import RiskManager
    return RiskManager(tracker)


@pytest.fixture
def default_config(monkeypatch):
    """Pin config to known values so tests aren't affected by .env."""
    from bot import config

    monkeypatch.setattr(config, "TIER1_MIN", 80_000.0)
    monkeypatch.setattr(config, "TIER1_MAX", 150_000.0)
    monkeypatch.setattr(config, "TIER1_SIZE", 1.0)
    monkeypatch.setattr(config, "TIER2_MAX", 300_000.0)
    monkeypatch.setattr(config, "TIER2_SIZE", 2.0)
    monkeypatch.setattr(config, "TIER3_SIZE", 3.0)
    monkeypatch.setattr(config, "MIN_ORDER_SIZE_USDC", 1.0)
    monkeypatch.setattr(config, "MAX_POSITION_SIZE_USDC", 100.0)
    monkeypatch.setattr(config, "MAX_TOTAL_EXPOSURE_USDC", 500.0)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT_USDC", 50.0)
    monkeypatch.setattr(config, "MAX_SLIPPAGE", 0.05)
    monkeypatch.setattr(config, "PAPER_TRADE", True)
    monkeypatch.setattr(config, "PAPER_FEE_BPS", 0.0)
    monkeypatch.setattr(config, "TARGET_ADDRESS", "0xtarget")
    return config


def make_trade(
    *,
    action: str = "BUY",
    market_id: str = "m1",
    asset_id: str = "a1",
    price: float = 0.5,
    size_usdc: float = 100.0,
    outcome: str = "Yes",
    trade_id: str = "tx1",
    question: str = "Will X happen?",
    timestamp: int = 1_700_000_000,
):
    """Helper for building Trade objects."""
    from bot.models import Trade

    return Trade(
        id=trade_id,
        market_id=market_id,
        question=question,
        side=action if action in ("BUY", "SELL") else "REDEEM",
        size_usdc=size_usdc,
        price=price,
        action=action,
        timestamp=timestamp,
        outcome=outcome,
        asset_id=asset_id,
    )


@pytest.fixture
def make_trade_fixture():
    return make_trade
