"""
Insider-flow algorithm — copies suspicious fresh-wallet whale buys.

The thesis (backed by every documented Polymarket insider case of 2025–26:
the Venezuela operation, the Google most-searched bet, the Iran strikes):
insiders show up as *brand-new wallets making large first bets at long odds
on news-driven markets*. They have no history, so no statistics over past
trades can find them — they have to be caught in the act.

How it works
------------
Polls the platform-wide Data-API `/trades` firehose with a server-side cash
filter, then funnels every row through a cheap-to-expensive filter chain:

  1. BUY side only
  2. entry odds ≤ `max_entry_odds` (long shots — favorites carry no signal)
  3. notional ≥ `min_cash_size_usdc` (defense in depth vs the API filter)
  4. title not matching `exclude_title_patterns` (sports floods the feed)
  5. our position not already at `bet_size_usdc` for this market
  6. ONLY THEN the network call: wallet freshness via one /activity page —
     young account, few prior trades, and verifiable. An unverifiable
     wallet (stats fetch failed) is NOT copied: fail closed.

Survivors become OpenIntents (top-up to `bet_size_usdc`). Exits are v1
hold-to-resolution: every `settle_check_every` polls, open positions are
checked against Gamma and resolved markets yield SettleIntents. Mirroring
the source wallet's SELL is a deliberate non-goal until paper data shows
whether these wallets dump before resolution.
"""

import logging
import time
from typing import Iterator, Optional

from bot import fetcher
from bot.algorithm import Algorithm, Intent, Mode, OpenIntent, SettleIntent
from bot.models import GlobalTrade

from .params import PARAMS, InsiderFlowParams

logger = logging.getLogger(__name__)


# Cap the dedupe ring so a long-running process can't leak memory.
SEEN_IDS_MAX = 5000

# Wallet freshness verdicts are cached briefly: one whale often hits the
# firehose several times in a burst, and every uncached check is a blocking
# /activity round-trip in the poll loop. Short enough that a verdict can't
# go meaningfully stale (freshness changes on a scale of days, not minutes).
WALLET_VERDICT_TTL_SECONDS = 300
WALLET_CACHE_MAX = 1000


class InsiderFlowAlgorithm(Algorithm):
    def __init__(
        self,
        name: Optional[str] = None,
        params: Optional[InsiderFlowParams] = None,
    ) -> None:
        if params is not None:
            self.params = params
        elif name is not None:
            from dataclasses import replace
            self.params = replace(PARAMS, name=name)
        else:
            self.params = PARAMS

        self._tracker = None        # PositionTracker, set in setup()
        self._paper: bool = self.params.mode == Mode.PAPER
        self._seen = fetcher.SeenRing(SEEN_IDS_MAX)
        self._poll_count = 0
        # wallet → (expires_at, stats-dict-if-fresh-else-None).
        # Failed lookups are never cached.
        self._wallet_verdicts: dict[str, tuple[float, Optional[dict]]] = {}

    @property
    def display_name(self) -> str:
        return f"{self.name} → fresh-wallet flow"

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def setup(self, tracker, notifier_mod, client) -> None:
        self._tracker = tracker
        # Live requires a CLOB client; fall back to paper rather than
        # silently mis-routing real-money orders (same rule as copy_trade).
        self._paper = self.params.mode == Mode.PAPER or client is None

        # Seed the dedupe ring with the current firehose tail so a restart
        # never replays trades that already happened.
        for row in fetcher.fetch_global_trades(
            self.params.min_cash_size_usdc, self.params.firehose_limit,
        ):
            self._seen.mark(self._key(row))
        logger.info(
            "[%s] Watching global flow ≥ $%.0f at odds ≤ %.2f "
            "(seeded %d existing rows).",
            self.params.name, self.params.min_cash_size_usdc,
            self.params.max_entry_odds, len(self._seen),
        )

    # ── Poll → Intents ──────────────────────────────────────────────────────

    def poll(self) -> Iterator[Intent]:
        self._poll_count += 1

        rows = fetcher.fetch_global_trades(
            self.params.min_cash_size_usdc, self.params.firehose_limit,
        )
        new_rows = [r for r in rows if self._key(r) not in self._seen]
        for row in sorted(new_rows, key=lambda r: r.timestamp):
            # Mark immediately — a rejected row stays rejected and must not
            # re-run its (possibly network-priced) checks every tick.
            self._seen.mark(self._key(row))
            yield from self._intent_for(row)

        if self._poll_count % self.params.settle_check_every == 0:
            yield from self._settle_sweep()

    # ── Filter chain ─────────────────────────────────────────────────────────

    def _intent_for(self, row: GlobalTrade) -> Iterator[OpenIntent]:
        p = self.params

        # Cheap, local gates first.
        if row.side != "BUY":
            return
        if not (0 < row.price <= p.max_entry_odds):
            return
        if row.cash_usdc < p.min_cash_size_usdc:
            return
        if any(pat in row.title for pat in p.exclude_title_patterns):
            logger.debug("[%s] Excluded by title pattern: %s",
                         p.name, row.title[:50])
            return

        # Position gate before the freshness lookup — it's free and skips
        # the common case of repeat buys in a market we already copied.
        position = self._tracker.get(row.market_id, self._paper)
        current_cost = position.total_cost_usdc if position else 0.0
        scaled = round(p.bet_size_usdc - current_cost, 8)
        if scaled < p.min_order_size_usdc:
            logger.info(
                "[%s] Already at bet size $%.2f (current $%.2f), skipping: %s",
                p.name, p.bet_size_usdc, current_cost, row.title[:50],
            )
            return

        # The one network-priced gate, last.
        stats = self._vet_wallet(row.wallet)
        if stats is None:
            return

        wallet_short = f"{row.wallet[:6]}…{row.wallet[-4:]}"
        logger.info(
            "[%s] SUSPICIOUS FLOW: fresh wallet %s bet $%.0f @ %.2f | %s",
            p.name, wallet_short, row.cash_usdc, row.price, row.title[:55],
        )
        yield OpenIntent(
            market_id=row.market_id,
            asset_id=row.asset_id,
            usdc_amount=scaled,
            signal_price=row.price,
            question=row.title,
            outcome=row.outcome,
            signal_id=self._key(row),
            reason=(
                f"fresh wallet {wallet_short} bet "
                f"${row.cash_usdc:,.0f} @ {row.price:.2f}"
            ),
            features=self._features_for(row, stats),
        )

    def _features_for(self, row: GlobalTrade, stats: dict) -> dict:
        """Raw observables at signal time — training data for confidence
        models. Raw inputs only (no derived scores); enrichment failures
        degrade to None, never gate the signal."""
        now = time.time()
        oldest = stats.get("oldest_ts")
        return {
            "odds": row.price,
            "cash_usdc": row.cash_usdc,
            "shares": row.shares,
            "wallet": row.wallet,
            "trade_count": stats.get("trade_count"),
            "activity_count": stats.get("activity_count"),
            "wallet_age_seconds": (now - oldest) if oldest else None,
            "detect_latency_seconds": max(0, int(now) - row.timestamp),
            "hour_utc": time.gmtime(now).tm_hour,
            "portfolio_value_usdc": fetcher.fetch_wallet_value(row.wallet),
        }

    def _vet_wallet(self, wallet: str) -> Optional[dict]:
        """Freshness gate. Returns the wallet's stats dict when it passes
        (young account, little history) so feature capture reuses the lookup;
        None when rejected. Unverifiable (lookup failed) → None and NOT
        cached — we never copy a wallet we couldn't vet (fail closed), but
        its next row deserves a retry."""
        now = time.time()
        cached = self._wallet_verdicts.get(wallet)
        if cached is not None and cached[0] > now:
            return cached[1]

        p = self.params
        stats = fetcher.fetch_wallet_stats(wallet)
        if stats is None:
            logger.warning(
                "[%s] Could not verify wallet %s — skipping (fail closed).",
                p.name, wallet,
            )
            return None

        verdict = stats if self._freshness_verdict(stats) else None
        if len(self._wallet_verdicts) >= WALLET_CACHE_MAX:
            self._wallet_verdicts = {
                w: v for w, v in self._wallet_verdicts.items() if v[0] > now
            }
        self._wallet_verdicts[wallet] = (
            now + WALLET_VERDICT_TTL_SECONDS, verdict,
        )
        return verdict

    def _freshness_verdict(self, stats: dict) -> bool:
        p = self.params
        if stats["capped"]:
            return False
        if stats["trade_count"] > p.max_prior_trades:
            return False
        oldest = stats["oldest_ts"]
        # oldest_ts of None = zero recorded activity: a wallet so new the
        # API hasn't propagated it. That's maximal freshness, not an error.
        if oldest is not None and oldest < time.time() - p.max_wallet_age_days * 86_400:
            return False
        return True

    # ── Exit: settle resolved markets ────────────────────────────────────────

    def _settle_sweep(self) -> Iterator[SettleIntent]:
        for pos in self._tracker.all_open(paper=self._paper):
            market = fetcher.fetch_market_resolution(pos.market_id)
            if market and fetcher.market_is_resolved(market):
                logger.info(
                    "[%s] Market resolved — settling: %s",
                    self.params.name, pos.question[:55],
                )
                yield SettleIntent(
                    market_id=pos.market_id,
                    question=pos.question,
                    outcome=pos.outcome,
                    signal_id=f"settle:{pos.market_id}:{self._poll_count}",
                    reason="market resolved (sweep)",
                )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _key(row: GlobalTrade) -> str:
        # One tx can carry multiple fills (taker crossing several makers);
        # the composite key keeps them distinct while staying idempotent.
        return f"{row.tx_hash}:{row.wallet}:{row.asset_id}:{row.side}"
