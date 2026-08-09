"""Ranking, watchlist, and leader-lot tests for multi-leader copy trading."""

from algorithms.copy_trade.ranker import ResolvedBet, rank_wallets
from algorithms.copy_trade.watchlist import WatchlistRepository
from bot.execution import lots as copy_lots
from tests.conftest import make_trade


def _bets(wallet="0xa", start=1):
    return [
        ResolvedBet(wallet, 0.40, 1.0, start, 0.8),
        ResolvedBet(wallet, 0.45, 1.0, start + 1, 0.8),
        ResolvedBet(wallet, 0.50, 1.0, start + 2, 0.8),
        ResolvedBet(wallet, 0.40, 1.0, start + 3, 0.8),
        ResolvedBet(wallet, 0.45, 1.0, start + 4, 0.8),
        ResolvedBet(wallet, 0.50, 1.0, start + 5, 0.8),
    ]


def test_ranker_uses_confidence_adjusted_edge_not_raw_pnl():
    stable = _bets("0xstable")
    lucky = [ResolvedBet("0xlucky", 0.01, 1.0, 1, 1.0)]
    ranked = rank_wallets(stable + lucky, min_resolved_bets=1,
                          confidence_z=1.645, min_copyability_score=0.0)
    assert ranked[0].wallet == "0xstable"
    assert ranked[0].edge_lower_bound > 0
    assert ranked[-1].wallet == "0xlucky"


def test_watchlist_does_not_activate_when_persistence_fails(fresh_db):
    repo = WatchlistRepository()
    repo.refresh("copy", _bets(), watchlist_size=1, min_resolved_bets=2,
                 confidence_z=1.645, min_copyability_score=0.0,
                 persistence_passed=False)
    assert repo.active_wallets("copy") == []


def test_watchlist_activates_best_eligible_wallet(fresh_db):
    repo = WatchlistRepository()
    repo.refresh("copy", _bets("0xa") + [
        ResolvedBet("0xb", 0.70, 1.0, 1, 0.9),
        ResolvedBet("0xb", 0.70, 0.0, 2, 0.9),
    ], watchlist_size=1, min_resolved_bets=2, confidence_z=1.645,
       min_copyability_score=0.0, persistence_passed=True)
    assert repo.active_wallets("copy") == ["0xa"]


def test_leader_close_consumes_only_that_leaders_lots(fresh_db):
    copy_lots.record_open("copy", "m1", "asset", "0xa", "a1", 10, 5)
    copy_lots.record_open("copy", "m1", "asset", "0xb", "b1", 20, 10)

    assert copy_lots.close_for_leader("copy", "m1", "0xa", 7) == 7
    assert copy_lots.remaining_shares("copy", "m1", "0xa") == 3
    assert copy_lots.remaining_shares("copy", "m1", "0xb") == 20


def test_multi_leader_mode_emits_attributed_open_intent(fresh_db, ledger):
    """The integrated copy algorithm reads active leaders from its watchlist."""
    from algorithms.copy_trade.algorithm import CopyTradeAlgorithm
    from tests.conftest import copy_trade_params

    class FakeMarketData:
        def __init__(self):
            self.rows = []

        def recent_trades(self, wallet):
            return list(self.rows)

        def target_position_value(self, wallet, market_id, expected_min=0):
            return 100_000.0

        def market(self, market_id):
            return None

        def price(self, asset_id):
            return None

        @staticmethod
        def market_outcome_is_final(market):
            return False

    repo = WatchlistRepository()
    repo.refresh("multi", _bets(), watchlist_size=1, min_resolved_bets=2,
                 confidence_z=1.645, min_copyability_score=0.0,
                 persistence_passed=True)
    data = FakeMarketData()
    class FakeHistory:
        def resolved_bets(self, wallets):
            return _bets()

    algo = CopyTradeAlgorithm(
        params=copy_trade_params(
            name="multi", watchlist_size=1, watchlist_candidate_wallets=("0xa",),
            watchlist_min_resolved_bets=2, tier1_size=1.0, max_position_size_usdc=3.0,
        ), market_data=data, watchlist=repo, ranker_source=FakeHistory(),
    )
    algo.setup(ledger)
    assert repo.active_wallets("multi") == ["0xa"]
    data.rows = [make_trade(action="BUY", trade_id="leader-buy", price=0.5)]

    intents = list(algo.poll())

    assert len(intents) == 1
    intent = intents[0]
    assert intent.leader_wallet == "0xa"
    assert intent.leader_event_id == "0xa:leader-buy"
