"""
Copy-trade algorithm — mirrors a Polymarket wallet's activity.

How it works
------------
Polls the Polymarket activity API for new trades by `params.target_address`,
classifies each row into one of {BUY, SELL, MERGE, REDEEM}, and yields the
corresponding Intent for the shared runner to execute.

Stateful pieces (kept per-instance so two copy-trade workers can target
different wallets in parallel):
  * `seen_ids`  — bounded LRU of trade tx hashes already processed.
  * `holding_cache` — last observed value of the target's holding per
    market, used to size mirror sells accurately.

Sizing strategy: tier-based, top-up to a fixed total.
  * Look up the target's total $ holding in the market.
  * Map to a tier (TIER1/TIER2/TIER3 size from params).
  * Top up our existing position so its total cost = tier target.
  * Skip if we're already at/above target (no add) or if the top-up is
    below the per-order minimum.

Close strategy:
  * SELL signal → fraction = target_sell_$ / cached_pre_sell_holding.
  * MERGE signal → fraction = 1.0 (target exited via complementary pairs).
  * REDEEM signal → emit a SettleIntent so the runner settles at the
    canonical close price.
"""

import collections
import logging
from typing import Iterator, Optional

from bot import fetcher
from bot.algorithm import Algorithm, CloseIntent, Intent, OpenIntent, SettleIntent

from .params import PARAMS

logger = logging.getLogger(__name__)


# Cap the dedupe ring so a long-running process can't leak memory.
SEEN_IDS_MAX = 5000


class CopyTradeAlgorithm(Algorithm):
    params = PARAMS

    def __init__(self) -> None:
        self._address: str = ""
        self._tracker = None        # PositionTracker, set in setup()
        self._paper: bool = True    # resolved in setup() from client + config
        self._seen_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.holding_cache = fetcher.TargetHoldingCache()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def setup(self, tracker, notifier_mod, client) -> None:
        self._tracker = tracker
        # The algorithm needs to know if we're in paper mode to read
        # tracker state with the right `paper` flag. Resolve from the
        # presence of a CLOB client (mirrors main.py's single resolution).
        from bot import config as _cfg
        self._paper = _cfg.PAPER_TRADE or client is None

        if self.params.target_address:
            self._address = self.params.target_address
        elif self.params.target_username:
            logger.info(
                "[%s] Looking up wallet for '%s'...",
                self.params.name, self.params.target_username,
            )
            self._address = fetcher.lookup_wallet(self.params.target_username) or ""
        if not self._address:
            raise RuntimeError(
                f"[{self.params.name}] No target wallet configured. "
                f"Set COPYTRADE_TARGET_ADDRESS or COPYTRADE_TARGET_USERNAME."
            )
        logger.info("[%s] Monitoring address: %s", self.params.name, self._address)

        # Seed dedupe ring with the current activity tail so we don't
        # re-execute everything from history on startup.
        for t in fetcher.fetch_recent_trades(self._address):
            if t.id:
                self._mark_seen(t.id)
        logger.info(
            "[%s] Seeded with %d existing trades. Watching for new ones...",
            self.params.name, len(self._seen_ids),
        )

    # ── Poll → Intents ──────────────────────────────────────────────────────

    def poll(self) -> Iterator[Intent]:
        trades = fetcher.fetch_recent_trades(self._address)
        # Skip trades smaller than the configured floor (dust filter).
        if self.params.min_trade_size_usdc > 0:
            trades = [t for t in trades if t.size_usdc >= self.params.min_trade_size_usdc]

        new_trades = [t for t in trades if t.id and t.id not in self._seen_ids]
        for t in sorted(new_trades, key=lambda x: x.timestamp):
            self._mark_seen(t.id)
            logger.info("[%s] New trade detected: %s", self.params.name, t)
            yield from self._intents_for(t)

    # ── Trade → Intent translation ──────────────────────────────────────────

    def _intents_for(self, t) -> Iterator[Intent]:
        if t.action == "BUY":
            yield from self._open_intent_for(t)
        elif t.action == "SELL":
            yield from self._close_intent_for(t)
        elif t.action == "MERGE":
            # Target exited via complementary-pair redemption — full close.
            self.holding_cache.set(t.market_id, 0.0)
            yield CloseIntent(
                market_id=t.market_id,
                fraction=1.0,
                signal_price=0.0,            # no slippage gate on forced exit
                question=t.question,
                outcome=t.outcome,
                signal_id=t.id,
                reason="target merged",
            )
        elif t.action == "REDEEM":
            yield SettleIntent(
                market_id=t.market_id,
                question=t.question,
                outcome=t.outcome,
                signal_id=t.id,
                reason="market resolved",
            )

    def _open_intent_for(self, t) -> Iterator[OpenIntent]:
        # Look up the target's total holding to pick a tier. `expected_min`
        # defeats the Data API's eventual-consistency window — the BUY we
        # just observed must be reflected.
        holding = fetcher.fetch_target_position_value(
            self._address, t.market_id, expected_min=t.size_usdc,
        )
        self.holding_cache.set(t.market_id, holding)

        logger.info(
            "[%s] Target holding in market: $%.0f | %s",
            self.params.name, holding, t.question[:55],
        )

        if holding < self.params.tier1_min:
            logger.info(
                "[%s] Holding $%.0f below tier 1 min $%.0f, skipping: %s",
                self.params.name, holding, self.params.tier1_min, t.question[:50],
            )
            return

        tier = self._tier_for_holding(holding)
        position = self._tracker.get(t.market_id, self._paper)
        current_cost = position.total_cost_usdc if position else 0.0
        scaled = round(tier - current_cost, 8)

        # Skip when the top-up would be *below* the minimum order. A top-up
        # exactly equal to min order is still a real buy — let it through;
        # the risk manager does its own (identical) min check downstream.
        if scaled < self.params.min_order_size_usdc:
            logger.info(
                "[%s] Already at tier target $%.2f (current $%.2f), skipping: %s",
                self.params.name, tier, current_cost, t.question[:50],
            )
            return

        logger.info(
            "[%s] Tier top-up: $%.2f (target $%.2f, current $%.2f, holding $%.0f) | %s",
            self.params.name, scaled, tier, current_cost, holding, t.question[:55],
        )

        yield OpenIntent(
            market_id=t.market_id,
            asset_id=t.asset_id or "",
            usdc_amount=scaled,
            signal_price=t.price,
            question=t.question,
            outcome=t.outcome,
            signal_id=t.id,
            reason=f"tier {tier} (target holding ${holding:,.0f})",
        )

    def _close_intent_for(self, t) -> Iterator[CloseIntent]:
        cached_pre = self.holding_cache.get(t.market_id)
        if cached_pre is None or cached_pre <= 0:
            logger.info(
                "[%s] No cached holding for %s — full close.",
                self.params.name, t.market_id[:12],
            )
            ratio = 1.0
        else:
            ratio = max(0.0, min(1.0, t.size_usdc / cached_pre))
            self.holding_cache.decrement(t.market_id, t.size_usdc)

        yield CloseIntent(
            market_id=t.market_id,
            fraction=ratio,
            signal_price=t.price,
            question=t.question,
            outcome=t.outcome,
            signal_id=t.id,
            reason=f"mirror sell ({ratio * 100:.0f}%)",
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _tier_for_holding(self, holding: float) -> float:
        """Map a USD holding to a tier bet size. Pure function for tests."""
        if holding <= self.params.tier1_max:
            return self.params.tier1_size
        if holding <= self.params.tier2_max:
            return self.params.tier2_size
        return self.params.tier3_size

    def _mark_seen(self, trade_id: str) -> None:
        if trade_id in self._seen_ids:
            self._seen_ids.move_to_end(trade_id)
        else:
            self._seen_ids[trade_id] = None
            if len(self._seen_ids) > SEEN_IDS_MAX:
                self._seen_ids.popitem(last=False)
