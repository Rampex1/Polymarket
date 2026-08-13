"""Snapshot-consensus engine — cohort agreement into intents.

Each snapshot re-derives the whole picture from the cohort's standing
positions, so there is no event buffer, no dedupe ring, and no holding cache
to drift out of sync: what the cohort holds right now *is* the state. A
missed poll costs nothing, and agreement accumulated days apart still counts.

Sizing reuses the tier ladder with the cohort's aggregate cost basis standing
in for a single target's holding — `tier1_min` becomes "how much smart money
has to be on this before it is worth copying".

Two ways out. The cohort walking away closes the position early — that is the
signal the strategy is actually built on, and it needs no diffing: support is
just a count off the current snapshot. Anything the cohort holds to the end is
closed by the shared settle sweep at the canonical resolution price.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Optional

from bot.domain.intents import CloseIntent, Intent, OpenIntent

from .consensus import find_consensus, parse_positions, support_by_asset

logger = logging.getLogger(__name__)

SNAPSHOT_WORKERS = 8


class ConsensusEngine:
    """Watch a wallet cohort's standing positions for agreement."""

    def __init__(self, params, market_data, watchlist, tier_for_holding):
        self.params = params
        self._market_data = market_data
        self._watchlist = watchlist
        self._tier_for_holding = tier_for_holding
        self._ledger = None
        self._paper = True
        self._last_snapshot = 0.0
        # market_id → {"cats": str, "end_ts": float|None}. Neither changes, so
        # no TTL. Lookup failures are not cached.
        self._market_info: dict[str, dict] = {}

    def setup(self, ledger, paper: bool) -> None:
        self._ledger, self._paper = ledger, paper
        cohort = self._cohort()
        if not cohort:
            logger.warning(
                "[%s] Consensus mode has an empty cohort — nothing to watch.",
                self.params.name,
            )
        logger.info(
            "[%s] Consensus mode: %d wallets, %d-wallet support / %d margin, "
            "snapshot every %ds.",
            self.params.name, len(cohort), self.params.consensus_min_leaders,
            self.params.consensus_min_margin, self.params.snapshot_interval_seconds,
        )

    def _cohort(self) -> list[str]:
        # The ranked cohort once the scorer fills it; the hand-curated
        # candidate list until then.
        return (self._watchlist.active_wallets(self.params.name)
                or [w.lower() for w in self.params.watchlist_candidate_wallets])

    # ── Poll → Intents ──────────────────────────────────────────────────────

    def poll(self) -> Iterator[Intent]:
        now = time.time()
        if now - self._last_snapshot < self.params.snapshot_interval_seconds:
            return
        self._last_snapshot = now

        cohort = self._cohort()
        if not cohort:
            return

        holdings = []
        with ThreadPoolExecutor(max_workers=SNAPSHOT_WORKERS) as pool:
            for wallet, rows in zip(cohort, pool.map(self._market_data.user_positions, cohort)):
                holdings.extend(parse_positions(wallet, rows))

        # Exits first: a freed position is exposure the entries below can use.
        yield from self._decay_exits(holdings, len(cohort))

        rows = find_consensus(
            holdings,
            min_support=self.params.consensus_min_leaders,
            min_margin=self.params.consensus_min_margin,
            min_conviction=self.params.consensus_min_conviction,
            max_price=self.params.consensus_max_price,
            min_price=self.params.consensus_min_price,
        )
        logger.info(
            "[%s] Snapshot: %d positions across %d wallets → %d consensus markets.",
            self.params.name, len(holdings), len({h.wallet for h in holdings}), len(rows),
        )
        for consensus in rows:
            yield from self._open(consensus, len(cohort))

    # ── Exit: the cohort walked away ────────────────────────────────────────

    def _decay_exits(self, holdings, cohort_size: int) -> Iterator[CloseIntent]:
        """Close positions the cohort has abandoned.

        The exit floor sits below the entry bar on purpose: without that band
        a position churns open and closed on a single leader trimming.
        """
        if self.params.consensus_exit_leaders <= 0:
            return

        # `user_positions` returns [] on a failed fetch by design — "couldn't
        # check this tick", not "holds nothing". A Data-API wobble therefore
        # reads as the entire cohort abandoning everything at once. Holding a
        # snapshot too long is survivable; dumping the whole book is not.
        responded = len({h.wallet for h in holdings})
        if responded < self.params.snapshot_min_responders * cohort_size:
            logger.warning(
                "[%s] Only %d/%d cohort wallets returned positions — skipping "
                "decay exits this snapshot.",
                self.params.name, responded, cohort_size,
            )
            return

        support = support_by_asset(holdings)
        for position in self._ledger.all_open(paper=self._paper):
            # No asset id means no way to tell which side we are on, and
            # `support.get("")` would read as zero and close everything.
            if not position.asset_id:
                continue
            info = self._lookup_market(position.market_id)
            if info is None:
                continue          # unverifiable — never act on a failed lookup
            hours = self._hours_left(info["end_ts"])
            # Past its end date the cohort's rows go `redeemable` and vanish
            # from the snapshot, so support reads zero for a market that is
            # merely resolving. Settlement owns those: selling into a resolved
            # book books P&L off a dead price.
            if hours is None or hours <= 0:
                continue
            held = support.get(position.asset_id, 0)
            if held > self.params.consensus_exit_leaders:
                continue
            logger.info(
                "[%s] Cohort support fell to %d (floor %d) — closing: %s",
                self.params.name, held, self.params.consensus_exit_leaders,
                position.question[:50],
            )
            yield CloseIntent(
                market_id=position.market_id,
                fraction=1.0,
                # Gate disabled, as on a MERGE: the reason to hold is gone, and
                # a slippage check can only strand us in the position.
                signal_price=0.0,
                question=position.question,
                outcome=position.outcome,
                signal_id=f"decay:{position.asset_id}:{int(self._last_snapshot)}",
                reason=f"cohort support fell to {held} "
                       f"(floor {self.params.consensus_exit_leaders})",
            )

    # ── Entry: the cohort agrees ────────────────────────────────────────────

    def _open(self, c, cohort_size: int) -> Iterator[OpenIntent]:
        skip = self._screened_out(c)
        if skip:
            logger.info("[%s] Skipping (%s): %s", self.params.name, skip, c.title[:50])
            return

        if c.drift > self.params.consensus_max_drift:
            logger.info(
                "[%s] Skipping (drift %+.0f%% past cohort basis %.3f): %s",
                self.params.name, c.drift * 100, c.avg_entry, c.title[:50],
            )
            return

        if c.cohort_cost_usdc < self.params.tier1_min:
            return

        position = self._ledger.get(c.market_id, self._paper)
        if position is None and len(self._ledger.all_open(paper=self._paper)) >= self.params.max_concurrent_positions:
            logger.info(
                "[%s] At max_concurrent_positions (%d), skipping: %s",
                self.params.name, self.params.max_concurrent_positions, c.title[:50],
            )
            return

        target = self._tier_for_holding(c.cohort_cost_usdc)
        current = position.total_cost_usdc if position else 0.0
        amount = round(target - current, 8)
        # Already at tier — the natural dedupe. No SeenRing needed: a market
        # we are fully sized in yields nothing on every later snapshot.
        if amount < self.params.min_order_size_usdc:
            return

        info = self._market_info.get(c.market_id) or {}
        logger.info(
            "[%s] Consensus %d-%d ($%.0f cohort) → $%.2f | %s",
            self.params.name, c.support, c.opposition, c.cohort_cost_usdc,
            amount, c.title[:50],
        )
        yield OpenIntent(
            market_id=c.market_id,
            asset_id=c.asset_id,
            usdc_amount=amount,
            signal_price=c.current_price,
            question=c.title,
            outcome=c.outcome,
            signal_id=f"consensus:{c.asset_id}:{int(self._last_snapshot)}",
            reason=f"{c.support}-wallet consensus vs {c.opposition} "
                   f"(${c.cohort_cost_usdc:,.0f} cohort basis)",
            # Raw observables only — drift and any score are recoverable from
            # these, and a stored score would freeze today's formula into the
            # training data.
            features={
                "support": c.support,
                "opposition": c.opposition,
                "cohort_size": cohort_size,
                "cohort_cost_usdc": c.cohort_cost_usdc,
                "cohort_avg_entry": c.avg_entry,
                "price_at_signal": c.current_price,
                "market_category": (info.get("cats") or "").split(",")[0] or None,
                "hours_to_resolution": self._hours_left(info.get("end_ts")),
                "wallets": ",".join(c.wallets),
            },
        )

    # ── Screening ───────────────────────────────────────────────────────────

    def _screened_out(self, c) -> Optional[str]:
        """Reason to skip this market, or None to allow it."""
        info = self._lookup_market(c.market_id)
        # No market row means the time-value gate cannot run at all — fail
        # closed, same as insider_flow.
        if info is None:
            return "no Gamma data"

        cats = info["cats"]
        # Absent category data fails *open*: it is a noise filter, and Gamma
        # simply carries no labels for some markets.
        if cats and any(bad in cats for bad in self.params.exclude_categories):
            return f"category {cats[:40]}"

        hours = self._hours_left(info["end_ts"])
        if hours is None:
            return "unknown end date"
        if hours < self.params.min_hours_to_resolution:
            return f"resolves in {hours:.1f}h"
        return None

    def _lookup_market(self, market_id: str) -> Optional[dict]:
        if market_id in self._market_info:
            return self._market_info[market_id]
        market = self._market_data.market(market_id)
        if market is None:
            return None
        info = {
            "cats": self._market_data.market_labels(market),
            "end_ts": self._market_data.market_end_ts(market),
        }
        if len(self._market_info) > 2000:
            self._market_info.clear()
        self._market_info[market_id] = info
        return info

    @staticmethod
    def _hours_left(end_ts) -> Optional[float]:
        return None if end_ts is None else (end_ts - time.time()) / 3600
