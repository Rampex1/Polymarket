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
from bot.algorithm import Algorithm, CloseIntent, Intent, Mode, OpenIntent, SettleIntent

from .params import PARAMS, CopyTradeParams

logger = logging.getLogger(__name__)


# Cap the dedupe ring so a long-running process can't leak memory.
SEEN_IDS_MAX = 5000


class CopyTradeAlgorithm(Algorithm):
    def __init__(
        self,
        name: Optional[str] = None,
        params: Optional[CopyTradeParams] = None,
    ) -> None:
        """Create a copy-trade worker.

        Two ways to construct:
          * `CopyTradeAlgorithm()` — uses the module-level PARAMS (env-driven
            defaults). Convenient for single-algorithm setups.
          * `CopyTradeAlgorithm(name="my_variant", params=CopyTradeParams(...))`
            — full control over each instance, so profiles can spin up
            multiple copy-trade workers (different wallets, different tiers,
            different paper/live modes) in the same process.
        """
        if params is not None:
            self.params = params
        elif name is not None:
            # Cheap rename: clone PARAMS with the new name.
            from dataclasses import replace
            self.params = replace(PARAMS, name=name)
        else:
            self.params = PARAMS

        self._address: str = ""
        self._tracker = None        # PositionTracker, set in setup()
        self._paper: bool = self.params.mode == Mode.PAPER
        self._seen_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.holding_cache = fetcher.TargetHoldingCache()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def setup(self, tracker, notifier_mod, client) -> None:
        self._tracker = tracker
        # Mode comes from this algorithm's own params — no global toggle.
        # Live mode also requires a CLOB client; if there's none we fall
        # back to paper to avoid silently mis-routing real-money orders.
        self._paper = self.params.mode == Mode.PAPER or client is None

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
        """Resize our position to match the target's post-sell tier.

        Symmetric counterpart to the BUY top-up: BUY tops the position up
        to the new (higher) tier's total cost; SELL resizes it down to the
        new (lower) tier's total cost. The size we sell is
            current_cost  −  tier_for_holding(post_sell_holding)
        clamped to [0, current_cost]. Three special cases:

          * Cache miss   → full close (safe default; without a cached
                           pre-sell value we can't infer the post-sell tier).
          * Below tier 1 → target has effectively exited, so we fully close
                           regardless of how big our position is.
          * No change    → if the new target ≥ our current cost, no-op.
        """
        cached_pre = self.holding_cache.get(t.market_id)
        if cached_pre is None or cached_pre <= 0:
            logger.info(
                "[%s] No cached holding for %s — full close.",
                self.params.name, t.market_id[:12],
            )
            yield CloseIntent(
                market_id=t.market_id, fraction=1.0, signal_price=t.price,
                question=t.question, outcome=t.outcome, signal_id=t.id,
                reason="full close (cache miss)",
            )
            return

        # Target's holding after this sell. The cache is reset to the new
        # value so subsequent sells in the same market re-tier correctly.
        post_sell_holding = max(0.0, cached_pre - t.size_usdc)
        self.holding_cache.set(t.market_id, post_sell_holding)

        # What our position should cost at the new tier. Below tier 1 min
        # means the target has effectively exited the bet → full close.
        if post_sell_holding < self.params.tier1_min:
            new_target_cost = 0.0
        else:
            new_target_cost = self._tier_for_holding(post_sell_holding)

        position = self._tracker.get(t.market_id, self._paper)
        if position is None or position.shares <= 0:
            return    # nothing held; nothing to close
        current_cost = position.total_cost_usdc

        if new_target_cost >= current_cost:
            logger.info(
                "[%s] Target still at/above our tier ($%.2f → $%.2f), no resize",
                self.params.name, current_cost, new_target_cost,
            )
            return

        # Sell exactly enough to leave us at the new tier target.
        fraction = (current_cost - new_target_cost) / current_cost
        logger.info(
            "[%s] Tier resize: sell %.0f%% (cost $%.2f → $%.2f, "
            "target holding $%.0f → $%.0f) | %s",
            self.params.name, fraction * 100, current_cost, new_target_cost,
            cached_pre, post_sell_holding, t.question[:50],
        )
        yield CloseIntent(
            market_id=t.market_id,
            fraction=fraction,
            signal_price=t.price,
            question=t.question,
            outcome=t.outcome,
            signal_id=t.id,
            reason=f"resize to ${new_target_cost:.2f} "
                   f"(target now ${post_sell_holding:,.0f})",
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
