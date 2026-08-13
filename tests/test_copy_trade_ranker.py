"""Ranking, watchlist, lot, and consensus-mode tests for cohort copy trading."""

import time

from algorithms.copy_trade.ranker import ResolvedBet, rank_wallets
from algorithms.copy_trade.watchlist import WatchlistRepository
from bot.execution import lots


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
    lots.record_open("copy", "m1", "asset", "0xa", "a1", 10, 5)
    lots.record_open("copy", "m1", "asset", "0xb", "b1", 20, 10)

    assert lots.close_for_source("copy", "m1", "0xa", 7) == 7
    assert lots.remaining_shares("copy", "m1", "0xa") == 3
    assert lots.remaining_shares("copy", "m1", "0xb") == 20


def _position(asset="yes", opposite="no", outcome="Yes", cost=30_000.0, avg=0.40, cur=0.42):
    return {
        "asset": asset, "oppositeAsset": opposite, "conditionId": "m1",
        "outcome": outcome, "title": "Will the Fed hold rates?",
        "size": cost / avg, "avgPrice": avg, "initialValue": cost,
        "curPrice": cur, "redeemable": False,
    }


class FakeCohortData:
    """Positions keyed by wallet, plus the Gamma lookups the screen needs."""

    def __init__(self, positions, end_ts, cats=""):
        self.positions, self.end_ts, self.cats = positions, end_ts, cats

    def user_positions(self, wallet, limit=500):
        return self.positions.get(wallet, [])

    def market(self, market_id):
        return {"id": market_id}

    def market_labels(self, market):
        return self.cats

    def market_end_ts(self, market):
        return self.end_ts

    def price(self, asset_id):
        return None

    @staticmethod
    def market_outcome_is_final(market):
        return False


def _consensus_algo(data, **overrides):
    from algorithms.copy_trade.algorithm import CopyTradeAlgorithm
    from tests.conftest import copy_trade_params

    return CopyTradeAlgorithm(
        params=copy_trade_params(
            name="consensus", watchlist_candidate_wallets=("0xa", "0xb", "0xc", "0xd"),
            consensus_min_leaders=4, consensus_min_margin=3, tier1_size=1.0,
            max_position_size_usdc=3.0, **overrides,
        ),
        market_data=data, watchlist=WatchlistRepository(),
    )


def test_consensus_mode_opens_when_the_cohort_agrees(fresh_db, ledger):
    cohort = {w: [_position()] for w in ("0xa", "0xb", "0xc", "0xd")}
    algo = _consensus_algo(FakeCohortData(cohort, end_ts=time.time() + 30 * 86_400))
    algo.setup(ledger)

    (intent,) = list(algo.poll())

    assert intent.market_id == "m1"
    assert intent.usdc_amount == 1.0                      # tier 1 on $120k cohort basis
    assert intent.features["support"] == 4
    assert intent.features["opposition"] == 0
    assert intent.features["cohort_cost_usdc"] == 120_000.0
    # Raw observables only — no derived drift or score frozen into the row.
    assert "drift" not in intent.features


def test_consensus_mode_will_not_chase_a_market_past_the_cohort_basis(fresh_db, ledger):
    # Cohort paid 0.40; it now trades at 0.60. Their edge was the entry.
    cohort = {w: [_position(cur=0.60)] for w in ("0xa", "0xb", "0xc", "0xd")}
    algo = _consensus_algo(FakeCohortData(cohort, end_ts=time.time() + 30 * 86_400),
                           consensus_max_drift=0.25)
    algo.setup(ledger)

    assert list(algo.poll()) == []


def test_consensus_mode_skips_excluded_categories_and_imminent_resolutions(fresh_db, ledger):
    cohort = {w: [_position()] for w in ("0xa", "0xb", "0xc", "0xd")}
    soon = FakeCohortData(cohort, end_ts=time.time() + 3600)
    algo = _consensus_algo(soon)
    algo.setup(ledger)
    assert list(algo.poll()) == []                        # resolves in 1h

    sporty = FakeCohortData(cohort, end_ts=time.time() + 30 * 86_400, cats="sports,nfl")
    algo = _consensus_algo(sporty)
    algo.setup(ledger)
    assert list(algo.poll()) == []


def test_consensus_mode_snapshot_interval_throttles_the_api(fresh_db, ledger):
    cohort = {w: [_position()] for w in ("0xa", "0xb", "0xc", "0xd")}
    algo = _consensus_algo(FakeCohortData(cohort, end_ts=time.time() + 30 * 86_400))
    algo.setup(ledger)

    first = list(algo.poll())
    assert len(first) == 1
    # Second poll is inside snapshot_interval_seconds — no snapshot, no intent,
    # and crucially no N-wallet API sweep on every 20s worker tick.
    assert list(algo.poll()) == []
