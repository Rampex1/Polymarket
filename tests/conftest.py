"""
Shared pytest fixtures.

Design rules:
  * Use a *real* SQLite database via tempfile — no DB mocking. The real
    engine catches integration bugs (PRAGMA, indexes, migrations) that a
    mock would hide.

  * Use real RiskManager, Ledger, Trade dataclasses. The only
    thing we stub is the HTTP boundary (Polymarket API).

  * Each test gets a fresh DB. Every Ledger fixture is bound to
    the `copy_trade` algorithm namespace so existing tests behave the
    same way they did before the multi-algorithm refactor.

  * Params values live in algorithms/<type>/params.py, so the factories
    below just construct the real schema and override what a test is about.
    A test asserting on a knob's value is asserting on production config.
"""

import os
import sys
from dataclasses import dataclass

import pytest

# Ensure project root is importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def copy_trade_params(**overrides):
    """A CopyTradeParams for tests. The schema carries every value now, so
    this only supplies what a test is actually about — plus a target, which
    the schema deliberately ships empty so a real profile must set one."""
    from algorithms.copy_trade import CopyTradeParams

    return CopyTradeParams(**{"target_address": "0xtarget", **overrides})


def insider_flow_params(**overrides):
    """An InsiderFlowParams for tests. Override what the test is about."""
    from algorithms.insider_flow import InsiderFlowParams

    return InsiderFlowParams(**overrides)


@pytest.fixture(autouse=True)
def _no_discord(monkeypatch):
    """Block the Discord transports outright so the suite can never post to a
    real channel — the committed profile TOMLs carry live webhook URLs. Tests
    that assert on message bodies patch these themselves."""
    from bot.discord import threads
    from bot.discord import webhook

    monkeypatch.setattr(webhook.http, "post", lambda *a, **kw: None)
    monkeypatch.setattr(threads._session, "post", lambda *a, **kw: None)


@pytest.fixture
def tmp_db_path(tmp_path):
    """Path to a fresh empty SQLite file."""
    return str(tmp_path / "test.db")


@pytest.fixture
def fresh_db(tmp_db_path, monkeypatch):
    """Point bot.db at a brand-new file for this test, then close + reset."""
    from bot import config
    import bot.storage.db as dbmod

    monkeypatch.setattr(config, "DB_PATH", tmp_db_path)
    dbmod.reset_for_tests()
    yield dbmod
    dbmod.reset_for_tests()


@pytest.fixture
def ledger(fresh_db):
    """A Ledger bound to a fresh DB, namespaced to copy_trade."""
    from bot.storage.ledger import Ledger

    t = Ledger(algo="copy_trade")
    t.init_paper_balance(10_000.0)
    return t


@dataclass
class _TestParams:
    """Minimal params object for RiskManager + runner-handler tests."""
    name: str = "copy_trade"
    # mode is set in the default_params fixture so we can import Mode there
    # without circular references on import order.
    mode: object = None
    max_position_size_usdc: float = 100.0
    max_total_exposure_usdc: float = 500.0
    daily_loss_limit_usdc: float = 50.0
    min_order_size_usdc: float = 1.0
    max_slippage: float = 0.05
    paper_starting_balance: float = 10_000.0
    paper_fee_bps: float = 0.0
    poll_interval_seconds: int = 0
    order_type: str = "market"
    # CopyTrade-specific knobs used by some tests.
    tier1_min: float = 80_000.0
    tier1_max: float = 150_000.0
    tier1_size: float = 1.0
    tier2_max: float = 300_000.0
    tier2_size: float = 2.0
    tier3_size: float = 3.0
    target_address: str = "0xtarget"
    target_username: str = ""
    min_trade_size_usdc: float = 0.0
    settle_check_every: int = 999  # large so sweep never fires unexpectedly in tests
    webhook_url: str = ""


@pytest.fixture
def default_params():
    """A fully-initialised _TestParams instance defaulting to PAPER mode."""
    from bot.domain.mode import Mode
    return _TestParams(mode=Mode.PAPER)


# Backwards-compatible alias — old tests referenced `default_config`.
@pytest.fixture
def default_config(default_params):
    return default_params


@pytest.fixture
def risk(ledger, default_params):
    from bot.execution.risk import RiskManager
    return RiskManager(ledger, default_params)


def make_global_trade(**overrides):
    """A parsed firehose row (GlobalTrade) that passes every InsiderFlow
    filter by default: big cash, long odds, non-sports title."""
    import time

    from bot.domain.records import GlobalTrade

    defaults = dict(
        tx_hash="0xtx1",
        wallet="0xwhale",
        side="BUY",
        price=0.20,
        shares=50_000.0,
        cash_usdc=10_000.0,
        market_id="m1",
        asset_id="a1",
        timestamp=int(time.time()),
        title="Will the ceasefire be announced this month?",
        outcome="Yes",
        trader_name="Quiet-Fox",
    )
    defaults.update(overrides)
    return GlobalTrade(**defaults)


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
    from bot.domain.records import Trade

    return Trade(
        id=trade_id,
        market_id=market_id,
        question=question,
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
