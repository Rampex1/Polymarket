"""Multi-leader event watcher and consensus-to-intent translation."""

import time
from typing import Callable, Iterator

from bot import copy_lots, fetcher
from bot.algorithm import CloseIntent, Intent, OpenIntent, SettleIntent


class MultiLeaderCopyEngine:
    """Watch an active ranked cohort while preserving leader exit attribution."""

    def __init__(self, params, market_data, watchlist, ranker_source, tier_for_holding: Callable[[float], float]):
        self.params = params
        self._market_data = market_data
        self._watchlist = watchlist
        self._ranker_source = ranker_source
        self._tier_for_holding = tier_for_holding
        self._tracker = None
        self._paper = True
        self._seen: dict[str, fetcher.SeenRing] = {}
        self._holdings: dict[str, fetcher.TargetHoldingCache] = {}
        self._recent_entries: list[tuple[float, str, str, str, str]] = []
        self._last_rank_refresh = 0.0

    def setup(self, tracker, paper: bool) -> None:
        self._tracker, self._paper = tracker, paper
        self._refresh_watchlist(force=True)
        self._seed_active_wallets()

    def _refresh_watchlist(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_rank_refresh < self.params.watchlist_refresh_seconds:
            return
        self._last_rank_refresh = now
        if self._ranker_source is None:
            return
        candidates = self.params.watchlist_candidate_wallets
        bets = self._ranker_source.resolved_bets(candidates)
        # The score source can pre-compute a persistence verdict when it owns a
        # broader historic dataset.  Local/injected sources default to the
        # ranker's conservative early/late test.
        from .ranker import persistence_passes
        passed = persistence_passes(
            bets, self.params.watchlist_min_resolved_bets,
            self.params.watchlist_confidence_z,
            self.params.watchlist_min_copyability_score,
            self.params.watchlist_size,
        )
        self._watchlist.refresh(
            self.params.name, bets, watchlist_size=self.params.watchlist_size,
            min_resolved_bets=self.params.watchlist_min_resolved_bets,
            confidence_z=self.params.watchlist_confidence_z,
            min_copyability_score=self.params.watchlist_min_copyability_score,
            persistence_passed=passed,
        )

    def _seed_active_wallets(self) -> None:
        for wallet in self._watchlist.active_wallets(self.params.name):
            if wallet in self._seen:
                continue
            seen = self._seen.setdefault(wallet, fetcher.SeenRing(fetcher.SEEN_IDS_MAX))
            self._holdings.setdefault(wallet, fetcher.TargetHoldingCache())
            for trade in self._market_data.recent_trades(wallet):
                if trade.id and trade.action not in ("REDEEM", "MERGE"):
                    seen.mark(trade.id)

    def poll(self) -> Iterator[Intent]:
        self._refresh_watchlist()
        self._seed_active_wallets()
        now = time.time()
        self._recent_entries = [e for e in self._recent_entries
                                if now - e[0] <= self.params.consensus_window_seconds]
        for wallet in self._watchlist.active_wallets(self.params.name):
            seen = self._seen.setdefault(wallet, fetcher.SeenRing(fetcher.SEEN_IDS_MAX))
            trades = self._market_data.recent_trades(wallet)
            for trade in sorted((t for t in trades if t.id and t.id not in seen), key=lambda t: t.timestamp):
                seen.mark(trade.id)
                if self.params.min_trade_size_usdc > 0 and trade.size_usdc < self.params.min_trade_size_usdc:
                    continue
                for intent in self._intents_for(wallet, trade):
                    yield intent

    def _intents_for(self, wallet: str, trade) -> Iterator[Intent]:
        if trade.action == "BUY":
            yield from self._open(wallet, trade)
        elif trade.action == "SELL":
            yield from self._close(wallet, trade, forced=False)
        elif trade.action == "MERGE":
            yield from self._close(wallet, trade, forced=True)
        elif trade.action == "REDEEM":
            yield SettleIntent(
                market_id=trade.market_id, question=trade.question, outcome=trade.outcome,
                signal_id=trade.id, reason=f"leader {wallet[:10]} redeemed",
            )

    def _open(self, wallet: str, trade) -> Iterator[OpenIntent]:
        holding = self._market_data.target_position_value(wallet, trade.market_id, trade.size_usdc) or 0.0
        self._holdings.setdefault(wallet, fetcher.TargetHoldingCache()).set(trade.market_id, holding)
        if holding < self.params.tier1_min:
            return
        self._recent_entries.append((time.time(), wallet, trade.market_id, trade.asset_id or "", trade.outcome))
        leaders = {row[1] for row in self._recent_entries
                   if row[2:] == (trade.market_id, trade.asset_id or "", trade.outcome)}
        target = self._tier_for_holding(holding)
        if len(leaders) >= self.params.consensus_min_leaders:
            target = min(self.params.max_position_size_usdc, target * self.params.consensus_size_multiplier)
        position = self._tracker.get(trade.market_id, self._paper)
        if position is None and len(self._tracker.all_open(paper=self._paper)) >= self.params.max_concurrent_positions:
            return
        current = position.total_cost_usdc if position else 0.0
        amount = round(target - current, 8)
        if amount < self.params.min_order_size_usdc:
            return
        yield OpenIntent(
            market_id=trade.market_id, asset_id=trade.asset_id or "", usdc_amount=amount,
            signal_price=trade.price, question=trade.question, outcome=trade.outcome,
            signal_id=f"{wallet}:{trade.id}", leader_wallet=wallet, leader_event_id=f"{wallet}:{trade.id}",
            reason=f"watchlist leader {wallet[:6]}… ({len(leaders)}-leader consensus)",
            features={"leader_wallet": wallet, "consensus_leaders": len(leaders), "leader_holding_usdc": holding},
        )

    def _close(self, wallet: str, trade, forced: bool) -> Iterator[CloseIntent]:
        position = self._tracker.get(trade.market_id, self._paper)
        leader_shares = copy_lots.remaining_shares(self.params.name, trade.market_id, wallet)
        if position is None or position.shares <= 0 or leader_shares <= 0:
            return
        cache = self._holdings.setdefault(wallet, fetcher.TargetHoldingCache())
        before = cache.get(trade.market_id)
        if forced or before is None or before <= 0:
            leader_fraction = 1.0
        else:
            after = max(0.0, before - trade.size_usdc)
            cache.set(trade.market_id, after)
            leader_fraction = min(1.0, trade.size_usdc / before)
        shares = min(position.shares, leader_shares * leader_fraction)
        if shares <= 0:
            return
        yield CloseIntent(
            market_id=trade.market_id, fraction=shares / position.shares,
            signal_price=0.0 if forced else trade.price, question=trade.question,
            outcome=trade.outcome, signal_id=f"{wallet}:{trade.id}",
            leader_wallet=wallet, leader_event_id=f"{wallet}:{trade.id}",
            reason=f"leader {wallet[:6]}… {'merged' if forced else 'sold'}",
        )
