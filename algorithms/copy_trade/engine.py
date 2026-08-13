"""Snapshot-consensus engine — cohort agreement into intents.

Each snapshot re-derives the whole picture from the cohort's standing
positions, so there is no event buffer, no dedupe ring, and no holding cache
to drift out of sync: what the cohort holds right now *is* the state. A
missed poll costs nothing, and agreement accumulated days apart still counts.

Sizing reuses the tier ladder with the cohort's aggregate cost basis standing
in for a single target's holding — `tier1_min` becomes "how much smart money
has to be on this before it is worth copying".

Exits are v1 hold-to-resolution, matching insider_flow: the settle sweep in
`CopyTradeAlgorithm` closes positions when Gamma reports the market resolved.
Exiting when the cohort walks away needs snapshot-over-snapshot diffing and
is deliberately left for Phase 2.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Optional

from bot.domain.intents import Intent, OpenIntent

from .consensus import find_consensus, parse_positions

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
