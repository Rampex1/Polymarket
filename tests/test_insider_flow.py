"""
InsiderFlow algorithm tests — fresh-wallet suspicious-flow detection.

Filter chain, freshness gate (fail-closed), dedupe, top-up sizing, and the
resolution settle-sweep. As everywhere else in the suite, only the HTTP
boundary (`bot.fetcher` functions) is stubbed; tracker/params run real code.
"""

import time
from dataclasses import replace

import pytest

from tests.conftest import make_global_trade, make_trade

from bot.algorithm import Mode, OpenIntent, SettleIntent


NOW = int(time.time())


def fresh_stats(**overrides):
    """Wallet stats for an unmistakably fresh wallet (hours old, 1 trade)."""
    stats = {
        "trade_count": 1,
        "activity_count": 1,
        "oldest_ts": NOW - 3600,
        "capped": False,
    }
    stats.update(overrides)
    return stats


def _params(**overrides):
    from algorithms.insider_flow.params import InsiderFlowParams

    base = dict(
        name="insider_flow_test",
        mode=Mode.PAPER,
        min_cash_size_usdc=5_000.0,
        max_entry_odds=0.35,
        max_wallet_age_days=14.0,
        max_prior_trades=10,
        bet_size_usdc=10.0,
        exclude_title_patterns=(" vs. ", " vs ", "O/U", "Spread"),
        settle_check_every=1_000_000,     # effectively off unless a test opts in
        firehose_limit=100,
        poll_interval_seconds=0,
        min_order_size_usdc=1.0,
        max_position_size_usdc=10.0,
        max_total_exposure_usdc=100.0,
        daily_loss_limit_usdc=50.0,
        max_slippage=0.10,
        order_type="market",
        paper_starting_balance=10_000.0,
        paper_fee_bps=0.0,
    )
    base.update(overrides)
    return InsiderFlowParams(**base)


@pytest.fixture
def algo(tracker):
    from algorithms.insider_flow import InsiderFlowAlgorithm

    a = InsiderFlowAlgorithm(params=_params())
    a._tracker = tracker
    a._paper = True
    return a


@pytest.fixture(autouse=True)
def _no_value_lookup(monkeypatch):
    """Default: portfolio-value enrichment returns None so no test ever makes
    a real HTTP call. Feature-capture tests override explicitly."""
    from bot import fetcher

    monkeypatch.setattr(
        fetcher, "fetch_wallet_value", lambda *a, **kw: None, raising=False,
    )


@pytest.fixture
def stub_firehose(monkeypatch):
    from bot import fetcher

    def _set(rows):
        monkeypatch.setattr(
            fetcher, "fetch_global_trades", lambda *a, **kw: list(rows),
        )
    return _set


@pytest.fixture
def stub_stats(monkeypatch):
    from bot import fetcher

    def _set(stats):
        monkeypatch.setattr(
            fetcher, "fetch_wallet_stats", lambda *a, **kw: stats,
        )
    return _set


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fresh_wallet_long_odds_buy_yields_open_intent(
    algo, stub_firehose, stub_stats
):
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats())

    intents = list(algo.poll())
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, OpenIntent)
    assert intent.usdc_amount == 10.0            # bet_size top-up from zero
    assert intent.signal_price == 0.20
    assert intent.market_id == "m1"
    assert intent.asset_id == "a1"
    assert intent.signal_id
    assert "fresh wallet" in intent.reason


def test_display_name_self_identifies():
    from algorithms.insider_flow import InsiderFlowAlgorithm

    a = InsiderFlowAlgorithm(params=_params())
    assert a.display_name == "insider_flow_test → fresh-wallet flow"


# ---------------------------------------------------------------------------
# Filter chain — each gate individually rejects
# ---------------------------------------------------------------------------


def test_skips_high_odds_buy(algo, stub_firehose, stub_stats):
    """Buying at 0.97 is a favorite/market-maker fill, not an insider signal."""
    stub_firehose([make_global_trade(price=0.97, cash_usdc=48_500.0)])
    stub_stats(fresh_stats())
    assert list(algo.poll()) == []


def test_skips_sell_rows(algo, stub_firehose, stub_stats):
    stub_firehose([make_global_trade(side="SELL")])
    stub_stats(fresh_stats())
    assert list(algo.poll()) == []


def test_skips_sub_minimum_cash(algo, stub_firehose, stub_stats):
    """Defense in depth — re-check cash locally even though the API filters."""
    stub_firehose([make_global_trade(cash_usdc=500.0, shares=2_500.0)])
    stub_stats(fresh_stats())
    assert list(algo.poll()) == []


def test_skips_sports_title_patterns(algo, stub_firehose, stub_stats):
    stub_firehose([
        make_global_trade(tx_hash="0xt1", title="Hurricanes vs. Golden Knights"),
        make_global_trade(tx_hash="0xt2", title="Games Total: O/U 2.5"),
    ])
    stub_stats(fresh_stats())
    assert list(algo.poll()) == []


def test_skips_old_wallet(algo, stub_firehose, stub_stats):
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats(oldest_ts=NOW - 60 * 86_400))    # 60 days old
    assert list(algo.poll()) == []


def test_skips_wallet_with_many_trades(algo, stub_firehose, stub_stats):
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats(trade_count=50))
    assert list(algo.poll()) == []


def test_skips_wallet_with_capped_history(algo, stub_firehose, stub_stats):
    """A full activity page means ≥ max_rows events — not fresh, regardless
    of what the (truncated) trade count says."""
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats(capped=True))
    assert list(algo.poll()) == []


def test_unverifiable_wallet_fails_closed(algo, stub_firehose, stub_stats):
    """Stats lookup failed (None) → do NOT copy a wallet we can't vet."""
    stub_firehose([make_global_trade()])
    stub_stats(None)
    assert list(algo.poll()) == []


def test_zero_history_wallet_is_fresh(algo, stub_firehose, stub_stats):
    """Brand-new wallet whose /activity hasn't propagated yet → fresh."""
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats(trade_count=0, activity_count=0, oldest_ts=None))
    assert len(list(algo.poll())) == 1


# ---------------------------------------------------------------------------
# API economy — don't burn a wallet-stats call on already-rejected rows
# ---------------------------------------------------------------------------


def test_wallet_stats_not_fetched_for_rejected_rows(
    algo, stub_firehose, monkeypatch
):
    from bot import fetcher

    calls = []
    monkeypatch.setattr(
        fetcher, "fetch_wallet_stats",
        lambda *a, **kw: calls.append(a) or fresh_stats(),
    )
    stub_firehose([make_global_trade(price=0.90, cash_usdc=45_000.0)])
    assert list(algo.poll()) == []
    assert calls == []


def test_wallet_verdict_cached_across_rows(algo, stub_firehose, monkeypatch):
    """Two rows from the same wallet in one burst → one /activity lookup.
    The verdict barely changes within the TTL, so re-fetching is pure waste
    that blocks the poll loop."""
    from bot import fetcher

    calls = []
    monkeypatch.setattr(
        fetcher, "fetch_wallet_stats",
        lambda *a, **kw: calls.append(a) or fresh_stats(),
    )
    stub_firehose([
        make_global_trade(tx_hash="0xt1", market_id="m1", asset_id="a1"),
        make_global_trade(tx_hash="0xt2", market_id="m2", asset_id="a2"),
    ])
    intents = list(algo.poll())
    assert len(intents) == 2
    assert len(calls) == 1


def test_failed_wallet_lookup_is_not_cached(algo, stub_firehose, monkeypatch):
    """A transient stats failure fails closed for THAT row only — the
    wallet's next row must retry the lookup, not inherit the failure."""
    from bot import fetcher

    results = iter([None, fresh_stats()])
    calls = []
    monkeypatch.setattr(
        fetcher, "fetch_wallet_stats",
        lambda *a, **kw: calls.append(a) or next(results),
    )
    stub_firehose([
        make_global_trade(tx_hash="0xt1", market_id="m1", asset_id="a1"),
        make_global_trade(tx_hash="0xt2", market_id="m2", asset_id="a2"),
    ])
    intents = list(algo.poll())
    assert len(intents) == 1                  # first row failed closed
    assert intents[0].market_id == "m2"       # second row retried and passed
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Feature capture — raw observables logged on every emitted intent
# ---------------------------------------------------------------------------


def test_intent_carries_raw_features(algo, stub_firehose, stub_stats, monkeypatch):
    """Features are raw observables (recompute derivations offline later);
    capture must include the signal economics and the wallet vetting data
    we already fetched."""
    from bot import fetcher

    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats())
    monkeypatch.setattr(
        fetcher, "fetch_wallet_value", lambda *a, **kw: 2500.0, raising=False,
    )

    intents = list(algo.poll())
    assert len(intents) == 1
    f = intents[0].features

    assert f["odds"] == 0.20
    assert f["cash_usdc"] == 10_000.0
    assert f["shares"] == 50_000.0
    assert f["wallet"] == "0xwhale"
    assert f["trade_count"] == 1
    assert f["activity_count"] == 1
    assert f["portfolio_value_usdc"] == 2500.0
    # fresh_stats puts oldest_ts an hour ago; allow scheduling slack.
    assert 3500 <= f["wallet_age_seconds"] <= 3800
    assert f["detect_latency_seconds"] >= 0
    assert 0 <= f["hour_utc"] <= 23


def test_value_lookup_failure_still_emits_intent(algo, stub_firehose, stub_stats):
    """Portfolio value is enrichment, not a gate — its failure must never
    cost us the signal."""
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats())
    # autouse fixture already stubs fetch_wallet_value → None

    intents = list(algo.poll())
    assert len(intents) == 1
    assert intents[0].features["portfolio_value_usdc"] is None


def test_zero_history_wallet_features_have_null_age(algo, stub_firehose, stub_stats):
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats(trade_count=0, activity_count=0, oldest_ts=None))

    intents = list(algo.poll())
    assert len(intents) == 1
    assert intents[0].features["wallet_age_seconds"] is None


# ---------------------------------------------------------------------------
# Import isolation — paper-mode profiles must not require py_clob_client
# ---------------------------------------------------------------------------


def test_insider_flow_imports_without_py_clob_client():
    """The algorithm must be importable when py_clob_client is absent —
    a paper-only deploy doesn't install live-trade deps, and an import
    failure here would crash the whole experimental profile at startup."""
    import os
    import subprocess
    import sys

    code = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'py_clob_client':\n"
        "            raise ImportError('blocked: ' + name)\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import algorithms.insider_flow.algorithm\n"
        "print('IMPORT_OK')\n"
    )
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=project_root, timeout=60,
    )
    assert "IMPORT_OK" in result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Dedupe + sizing
# ---------------------------------------------------------------------------


def test_same_row_not_processed_twice(algo, stub_firehose, stub_stats):
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats())

    assert len(list(algo.poll())) == 1
    assert list(algo.poll()) == []      # same tx hash → deduped


def test_setup_seeds_dedupe_ring(tracker, stub_firehose, stub_stats):
    """Rows already in the firehose at startup must not be replayed."""
    from algorithms.insider_flow import InsiderFlowAlgorithm

    a = InsiderFlowAlgorithm(params=_params())
    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats())

    a.setup(tracker, None, None)
    assert list(a.poll()) == []


def test_position_at_bet_size_skips_without_stats_call(
    algo, stub_firehose, tracker, monkeypatch
):
    """Already at bet size in this market → skip (and don't even vet the
    wallet — position check is the cheaper gate)."""
    from bot import fetcher

    seed = make_trade(action="BUY", market_id="m1", asset_id="a1", price=0.20)
    tracker.record_buy(seed, spent_usdc=10.0, shares=50.0, fill_price=0.20, paper=True)

    calls = []
    monkeypatch.setattr(
        fetcher, "fetch_wallet_stats",
        lambda *a, **kw: calls.append(a) or fresh_stats(),
    )
    stub_firehose([make_global_trade()])
    assert list(algo.poll()) == []
    assert calls == []


def test_partial_position_tops_up_to_bet_size(
    algo, stub_firehose, stub_stats, tracker
):
    seed = make_trade(action="BUY", market_id="m1", asset_id="a1", price=0.20)
    tracker.record_buy(seed, spent_usdc=4.0, shares=20.0, fill_price=0.20, paper=True)

    stub_firehose([make_global_trade()])
    stub_stats(fresh_stats())

    intents = list(algo.poll())
    assert len(intents) == 1
    assert abs(intents[0].usdc_amount - 6.0) < 1e-9


# ---------------------------------------------------------------------------
# Settle sweep — exit via market resolution
# ---------------------------------------------------------------------------


@pytest.fixture
def algo_sweeping(tracker):
    from algorithms.insider_flow import InsiderFlowAlgorithm

    a = InsiderFlowAlgorithm(params=_params(settle_check_every=1))
    a._tracker = tracker
    a._paper = True
    return a


def test_settle_sweep_emits_settle_for_resolved_market(
    algo_sweeping, stub_firehose, tracker, monkeypatch
):
    from bot import fetcher

    seed = make_trade(action="BUY", market_id="m1", asset_id="a1", price=0.20)
    tracker.record_buy(seed, spent_usdc=10.0, shares=50.0, fill_price=0.20, paper=True)

    stub_firehose([])
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution",
        lambda mid: {"closed": True, "outcomePrices": '["1", "0"]'},
    )

    intents = list(algo_sweeping.poll())
    assert len(intents) == 1
    assert isinstance(intents[0], SettleIntent)
    assert intents[0].market_id == "m1"


def test_settle_sweep_leaves_unresolved_markets_alone(
    algo_sweeping, stub_firehose, tracker, monkeypatch
):
    from bot import fetcher

    seed = make_trade(action="BUY", market_id="m1", asset_id="a1", price=0.20)
    tracker.record_buy(seed, spent_usdc=10.0, shares=50.0, fill_price=0.20, paper=True)

    stub_firehose([])
    monkeypatch.setattr(
        fetcher, "fetch_market_resolution", lambda mid: {"closed": False},
    )
    assert list(algo_sweeping.poll()) == []


# ---------------------------------------------------------------------------
# Params — env overrides + fail-safe default mode
# ---------------------------------------------------------------------------


def test_params_env_overrides(monkeypatch):
    from algorithms.insider_flow.params import InsiderFlowParams

    monkeypatch.setenv("INSIDERFLOW_MIN_CASH", "12000")
    monkeypatch.setenv("INSIDERFLOW_MAX_ODDS", "0.5")
    monkeypatch.setenv("INSIDERFLOW_MODE", "live")

    p = InsiderFlowParams()
    assert p.min_cash_size_usdc == 12_000.0
    assert p.max_entry_odds == 0.5
    assert p.mode == Mode.LIVE


def test_params_default_mode_is_paper(monkeypatch):
    from algorithms.insider_flow.params import InsiderFlowParams

    monkeypatch.delenv("INSIDERFLOW_MODE", raising=False)
    assert InsiderFlowParams().mode == Mode.PAPER
