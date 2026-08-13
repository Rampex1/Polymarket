"""
Copy-trade algorithm — mirrors a Polymarket wallet's activity.

Two modes, chosen by whether `watchlist_candidate_wallets` is set:

  * Single target (empty)  — everything documented below: follow one wallet's
    trade feed, tier off its holding.
  * Consensus (non-empty)  — hand off to `ConsensusEngine`, which snapshots a
    whole cohort's standing positions and trades where they agree. Entries
    come from the engine; exits are the same settle sweep used below.

How single-target mode works
----------------------------
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

import logging
from typing import Iterator, Optional

from bot.caches import SeenRing, TargetHoldingCache
from bot.domain.algorithm import Algorithm
from bot.domain.intents import (
    CloseIntent,
    Intent,
    OpenIntent,
    SettleIntent,
)
from bot.domain.mode import Mode
from bot.execution import settlement
from bot.polymarket import DEFAULT_MARKET_DATA, MarketDataGateway

from .params import CopyTradeParams
from .engine import ConsensusEngine
from .watchlist import WatchlistRepository

logger = logging.getLogger(__name__)


# Cap the dedupe ring so a long-running process can't leak memory.
SEEN_IDS_MAX = 5000


# Personal smoke-test wallet — when targeted, the algorithm switches to
# 1:1 mirroring so trades placed manually from that wallet exercise the
# bot's order-placement path without the $80k tier floor swallowing them.
# Lowercased for case-insensitive comparison.
_SMOKE_TEST_WALLET = "0x8b181a0f7ab8f2d886b9bb2765eb1699b6ecced9"


class CopyTradeAlgorithm(Algorithm):
    """Mirror one target wallet's trades, or a ranked cohort of wallets."""

    def __init__(
        self,
        params: CopyTradeParams,
        market_data: Optional[MarketDataGateway] = None,
        watchlist: Optional[WatchlistRepository] = None,
    ) -> None:
        """Create a copy-trade worker.

        `params` is required and carries every knob — there are no schema
        defaults to fall back on. Normally built by the profile loader from
        config/<profile>.toml, so one profile can spin up several copy-trade
        workers (different wallets, tiers, paper/live modes) in one process.
        """
        self.params = params

        self._market_data = market_data or DEFAULT_MARKET_DATA
        self._watchlist = watchlist or WatchlistRepository()
        self._multi = ConsensusEngine(
            self.params, self._market_data, self._watchlist, self._tier_for_holding,
        ) if self.params.watchlist_candidate_wallets else None

        self._address: str = ""
        self._ledger = None        # Ledger, set in setup()
        self._paper: bool = self.params.mode == Mode.PAPER
        self._seen_ids = SeenRing(SEEN_IDS_MAX)
        self.holding_cache = TargetHoldingCache()
        self._poll_count = 0

    @property
    def display_name(self) -> str:
        """Algorithm name plus the target being copied, so Discord
        messages from multiple copy-trade workers are self-identifying.
        Prefers the configured username; falls back to a shortened
        address if only the wallet was supplied.
        """
        if self._multi is not None:
            return f"{self.name} → {len(self.params.watchlist_candidate_wallets)}-wallet consensus"
        target = self.params.target_username
        if not target:
            addr = self.params.target_address
            target = f"{addr[:6]}…{addr[-4:]}" if len(addr) > 10 else addr
        return f"{self.name} → {target}" if target else self.name

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def setup(self, ledger) -> None:
        self._ledger = ledger
        # Mode comes from this algorithm's own params — no global toggle.
        self._paper = self.params.mode == Mode.PAPER

        if self._multi is not None:
            self._multi.setup(ledger, self._paper)
            return

        if self.params.target_address:
            self._address = self.params.target_address
        elif self.params.target_username:
            logger.info(
                "[%s] Looking up wallet for '%s'...",
                self.params.name, self.params.target_username,
            )
            self._address = self._market_data.lookup_wallet(self.params.target_username) or ""
        if not self._address:
            raise RuntimeError(
                f"[{self.params.name}] No target wallet configured. Set "
                f"target_address or target_username under [algorithm.params] "
                f"in the profile's config/<profile>.toml."
            )
        logger.info("[%s] Monitoring address: %s", self.params.name, self._address)

        # Seed dedupe ring with recent BUYs and SELLs so we don't re-execute
        # them on startup. REDEEM and MERGE are intentionally excluded: if
        # one happened while the bot was offline the dedupe ring would mark
        # it as seen and the missed exit would never be processed. Re-running
        # a REDEEM/MERGE against a position that was already closed is a no-op
        # (runner checks shares > 0), so replaying them is safe.
        for t in self._market_data.recent_trades(self._address):
            if t.id and t.action not in ("REDEEM", "MERGE"):
                self._seen_ids.mark(t.id)
        logger.info(
            "[%s] Seeded with %d existing trades. Watching for new ones...",
            self.params.name, len(self._seen_ids),
        )

    # ── Poll → Intents ──────────────────────────────────────────────────────

    def poll(self) -> Iterator[Intent]:
        self._poll_count += 1
        if self._multi is not None:
            yield from self._multi.poll()
            if self._poll_count == 1 or self._poll_count % self.params.settle_check_every == 0:
                yield from self._settle_sweep()
            return
        trades = self._market_data.recent_trades(self._address)
        # Skip trades smaller than the configured floor (dust filter).
        if self.params.min_trade_size_usdc > 0:
            trades = [t for t in trades if t.size_usdc >= self.params.min_trade_size_usdc]

        new_trades = [t for t in trades if t.id and t.id not in self._seen_ids]

        # One open per market per poll cycle. The target may place many small
        # buys in the same market in quick succession (all appear as "new"
        # trades on restart if they fell outside the dedupe-ring seed window).
        # Without this guard, each trade spawns a top-up attempt — the first
        # one's fill isn't recorded yet, so every subsequent one also sees
        # current_cost < tier_target and tries to buy again.
        opened_this_poll: set[str] = set()

        for t in sorted(new_trades, key=lambda x: x.timestamp):
            self._seen_ids.mark(t.id)
            logger.info("[%s] New trade detected: %s", self.params.name, t)
            for intent in self._intents_for(t):
                if isinstance(intent, OpenIntent):
                    if intent.market_id in opened_this_poll:
                        logger.info(
                            "[%s] Skipping duplicate open for same market this poll: %s",
                            self.params.name, (intent.question or intent.market_id)[:55],
                        )
                        continue
                    opened_this_poll.add(intent.market_id)
                yield intent

        if self._poll_count == 1:
            yield from self._startup_buy_sweep()
        if self._poll_count == 1 or self._poll_count % self.params.settle_check_every == 0:
            yield from self._settle_sweep()

    # ── Entry safety net: fill gaps from downtime ────────────────────────────

    def _startup_buy_sweep(self) -> Iterator[OpenIntent]:
        """On first poll, reconcile target's live positions against ours.

        The dedupe ring seeds BUY trades on startup so we don't re-execute
        positions we already hold — but that same seeding suppresses any BUY
        that happened while the bot was offline. This sweep bypasses the ring:
        it looks at what the target *currently holds* and tops us up to the
        matching tier for any market where we're short. signal_price=0 disables
        the slippage gate (no signal price available at startup).
        """
        if self._address.lower() == _SMOKE_TEST_WALLET:
            return

        target_positions = self._market_data.user_positions(self._address)
        # Group by market_id — a wallet can hold both sides; take the largest.
        by_market: dict[str, dict] = {}
        for row in target_positions:
            market_id = row.get("conditionId") or row.get("market_id")
            if not market_id:
                continue
            val = float(row.get("currentValue") or row.get("value") or 0)
            if val > float((by_market.get(market_id) or {}).get("currentValue") or 0):
                by_market[market_id] = row

        for market_id, row in by_market.items():
            holding = float(row.get("currentValue") or row.get("value") or 0)
            if holding < self.params.tier1_min:
                continue

            self.holding_cache.set(market_id, holding)
            tier = self._tier_for_holding(holding)
            our_position = self._ledger.get(market_id, self._paper)
            current_cost = our_position.total_cost_usdc if our_position else 0.0

            top_up = round(tier - current_cost, 8)
            if top_up < self.params.min_order_size_usdc:
                continue

            question = row.get("title") or (our_position.question if our_position else "") or ""
            outcome = row.get("outcome") or (our_position.outcome if our_position else "") or ""
            asset_id = row.get("asset") or ""

            logger.info(
                "[%s] Startup gap: target $%.0f, ours $%.2f → topping up $%.2f | %s",
                self.params.name, holding, current_cost, top_up, question[:55],
            )
            yield OpenIntent(
                market_id=market_id,
                asset_id=asset_id,
                usdc_amount=top_up,
                signal_price=0.0,
                question=question,
                outcome=outcome,
                signal_id=f"startup:{market_id}",
                reason=f"startup gap fill (target ${holding:,.0f}, ours ${current_cost:.2f})",
            )

    # ── Exit safety net: settle resolved markets ─────────────────────────────

    def _settle_sweep(self) -> Iterator[SettleIntent]:
        """Safety net for REDEEMs missed while the bot was offline."""
        yield from settlement.sweep_resolved(
            self._ledger, self._market_data, self._paper,
            self.params.name, self._poll_count,
        )

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
        # Smoke-test wallet → mirror the trade dollar-for-dollar with no
        # tier floor, so manually-placed test trades exercise the full
        # order path even at small sizes.
        if self._address.lower() == _SMOKE_TEST_WALLET:
            logger.info(
                "[%s] Smoke-test mirror BUY: $%.2f | %s",
                self.params.name, t.size_usdc, t.question[:55],
            )
            yield OpenIntent(
                market_id=t.market_id,
                asset_id=t.asset_id or "",
                usdc_amount=t.size_usdc,
                signal_price=t.price,
                question=t.question,
                outcome=t.outcome,
                signal_id=t.id,
                reason="smoke-test 1:1 mirror",
            )
            return

        # Look up the target's total holding to pick a tier. `expected_min`
        # defeats the Data API's eventual-consistency window — the BUY we
        # just observed must be reflected.
        holding = self._market_data.target_position_value(
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
        position = self._ledger.get(t.market_id, self._paper)
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
        # Smoke-test wallet → full close on any SELL. Simple and unambiguous:
        # one BUY signal opens, one SELL signal closes.
        if self._address.lower() == _SMOKE_TEST_WALLET:
            logger.info(
                "[%s] Smoke-test mirror SELL: full close | %s",
                self.params.name, t.question[:55],
            )
            yield CloseIntent(
                market_id=t.market_id, fraction=1.0, signal_price=t.price,
                question=t.question, outcome=t.outcome, signal_id=t.id,
                reason="smoke-test 1:1 mirror (full close)",
            )
            return

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

        position = self._ledger.get(t.market_id, self._paper)
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
