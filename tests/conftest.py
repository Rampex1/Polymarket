"""
Shared pytest fixtures.

Design rules for this test suite:

  * Use a *real* SQLite database via tempfile — no DB mocking. SQLite is
    fast enough that there's no reason to fake it, and using the real
    engine catches integration bugs (PRAGMA, indexes, migrations) that a
    mock would hide.

  * Use *real* dataclasses, real RiskManager, real PositionTracker. The
    only thing we stub is the HTTP boundary, because (a) we obviously
    can't hit the live Polymarket API in tests, and (b) we want to drive
    deterministic responses to exercise edge cases.

  * Each test gets a fresh DB — no leakage between tests.
"""

import os
import sys
import tempfile

import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db_path(tmp_path):
    """Path to a fresh empty SQLite file. Caller is responsible for pointing
    bot.config.DB_PATH at this and resetting bot.db._conn."""
    return str(tmp_path / "test.db")


@pytest.fixture
def fresh_db(tmp_db_path, monkeypatch):
    """Reset the bot.db module against a fresh DB file for a single test."""
    from bot import config
    import bot.db as dbmod

    monkeypatch.setattr(config, "DB_PATH", tmp_db_path)
    # Force re-init of the cached connection against the new path.
    dbmod._conn = None
    yield dbmod
    # Close after the test so the file handle releases and tmp_path can be cleaned up.
    if dbmod._conn is not None:
        try:
            dbmod._conn.close()
        except Exception:
            pass
        dbmod._conn = None


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
    """Pin the config to known values so tests aren't affected by .env."""
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
    """Test-helper to build Trade objects without keyword soup at call sites."""
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
    """Expose make_trade as a fixture so tests can do `make_trade()`."""
    return make_trade
