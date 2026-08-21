"""
Resolution-carry — buy near-certain outcomes, hold to resolution, collect
the residual.

The thesis is that the price is *correct*, not that we know better: at an
ask of 0.98 we pay 98c for a contract that pays $1, and the ~2% is what a
counterparty pays to get their capital back before settlement. We earn carry
for holding to resolution, the way a bond earns carry to maturity.

How it works
------------
Every poll scans Gamma's volume-ordered open markets inside the resolution
window and funnels each row through:

  1. price band — `bestAsk` in [min_ask, max_ask], the ask being what we
     would actually pay (`lastTradePrice` is stale, the mid is unfillable)
  2. spread and `liquidityClob` — a wide book means there is no real price
  3. time value — resolves within max_days_to_resolution, not sooner than
     min_hours_to_resolution, and the win-case return annualised over the
     wait beats the hurdle. Unknown end date fails closed.
  4. category — sports-primary, one cached Gamma lookup per market
  5. diversification — per-event and per-category caps, applied across held
     positions and this poll's own picks together

Survivors are ranked by annualised return (return per unit of capital-time
is the whole point) and the best few that fit the caps become OpenIntents at
a flat stake.

Two things here differ from the sibling strategies, both deliberate:

* **Sports is required, not excluded.** Everywhere else efficient pricing
  destroys a forecasting edge; here efficiency is the product, and sports
  brings objective resolution, genuinely independent events, and the short
  horizons that make the same 2% worth far more.
* **Poll cadence is 60s.** Measured p95 drift on a market already at 0.95+
  is 0.0185 over five minutes — roughly the entire return of a 0.98 entry.

Exits are v1 hold-to-resolution via the shared settle sweep. There is no
stop-loss on purpose: a stop realises exactly the losses this strategy
exists to absorb, and at 2% a win a handful of unnecessary cuts erases
dozens of successes. New lows are logged instead, so the question can be
settled with data rather than intuition.
"""

import logging
import time
from collections import Counter
from typing import Iterator, Optional

from bot.domain.algorithm import Algorithm
from bot.domain.intents import Intent, OpenIntent, SettleIntent
from bot.domain.mode import Mode
from bot.execution import settlement
from bot.polymarket import DEFAULT_MARKET_DATA, MarketDataGateway
from bot.storage import orders

from . import scan, screen
from .params import ResolutionCarryParams

logger = logging.getLogger(__name__)

# This process runs for weeks, so every dict keyed by market id is a slow leak
# unless something bounds it. The box has ~500MB and hosts two other Python
# processes, so "small and unbounded" is still unbounded.
BUCKET_CACHE_MAX = 500
SIGNALLED_CACHE_MAX = 500


class ResolutionCarryAlgorithm(Algorithm):
    """Buy the top of the book and get paid to wait for settlement."""

    def __init__(
        self,
        params: ResolutionCarryParams,
        market_data: Optional[MarketDataGateway] = None,
    ) -> None:
        self.params = params
        self._market_data = market_data or DEFAULT_MARKET_DATA
        self._ledger = None        # Ledger, set in setup()
        self._paper: bool = params.mode == Mode.PAPER
        self._poll_count = 0
        # market_id → (event_id, category). Neither changes, so no TTL;
        # primed on every open so a running process never re-fetches.
        self._buckets: dict[str, tuple[str, Optional[str]]] = {}
        # market_id → lowest ask seen while we held it. In memory only: this
        # is evidence for a future stop-loss decision, not a trading input.
        self._low_water: dict[str, float] = {}
        # market_id → earliest time we may signal it again. See _mark_signalled.
        self._signalled: dict[str, float] = {}

    @property
    def display_name(self) -> str:
        return f"{self.name} → resolution carry"

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def setup(self, ledger) -> None:
        self._ledger = ledger
        self._paper = self.params.mode == Mode.PAPER
        p = self.params
        logger.info(
            "[%s] Carrying %.3f–%.3f asks resolving in %.0fh–%.0fd "
            "(hurdle %.0f%%/yr, %s), $%.2f per position, max %d open.",
            p.name, p.min_ask, p.max_ask, p.min_hours_to_resolution,
            p.max_days_to_resolution, p.min_annualized_return * 100,
            "sports only" if p.require_sports else "any category",
            p.bet_size_usdc, p.max_concurrent_positions,
        )

    # ── Poll → Intents ───────────────────────────────────────────────────────

    def poll(self) -> Iterator[Intent]:
        self._poll_count += 1
        p = self.params

        # Settle first: the runner dispatches each intent as it is yielded,
        # so positions that resolved free their slot for this same poll.
        if self._poll_count % p.settle_check_every == 0:
            yield from self._settle_sweep()

        held = self._ledger.all_open(paper=self._paper)
        # A maker order that is resting is committed capital holding a slot,
        # but it is not a position — so without counting it here the scan
        # would re-emit the same market every 15s and stack orders on it,
        # while the per-event cap counted none of them.
        working = orders.held_market_ids(p.name, self._paper)
        slots = p.max_concurrent_positions - len(held) - len(working)
        if slots <= 0:
            logger.info("[%s] At %d/%d positions (%d resting) — not scanning.",
                        p.name, len(held), p.max_concurrent_positions, len(working))
            return

        held_ids = {pos.market_id for pos in held} | working
        now = time.time()
        candidates = []

        # One sweep per interval, shared with the other arms — they trade the
        # same universe, so scanning it once per arm was three times the HTTP
        # for identical rows. `sweep.rows` is already filtered to the widest
        # band any arm trades; every other gate still runs here, per arm.
        sweep = scan.SHARED.get(self._market_data, ttl=p.poll_interval_seconds)
        scanned = sweep.scanned
        funnel: Counter = Counter(sweep.funnel)
        self._note_lows(sweep.asks, held_ids)

        for row in sweep.rows:
            verdict = screen.evaluate(
                row, self._market_data.market_end_ts(row), now, p,
            )
            if isinstance(verdict, str):
                funnel[verdict] += 1
                continue
            # Holding the market is the primary dedupe: a position already
            # at size yields nothing on every later scan.
            if verdict.market_id in held_ids:
                funnel["already held"] += 1
                continue
            # ...but a signal the runner *rejected* leaves no position, so
            # it would come back on every poll. Dispatch alerts before it
            # gates, so a parked market failing the slippage check would
            # post to Discord every cycle for as long as it sits in band.
            if self._signalled.get(verdict.market_id, 0.0) > now:
                funnel["cooling off"] += 1
                continue
            labels = self._market_data.market_labels(row)
            if not screen.category_ok(labels, p):
                funnel["category"] += 1
                continue
            candidates.append(screen.with_category(verdict, labels))

        logger.info(
            "[%s] Scanned %d markets → %d in band. Rejections: %s",
            p.name, scanned, len(candidates),
            ", ".join(f"{k} {v}" for k, v in funnel.most_common(5)) or "none",
        )

        events, categories = self._held_buckets(held)
        for cand in screen.select(candidates, events, categories, slots, p):
            self._remember_bucket(cand.market_id, (cand.event_id, cand.category))
            self._mark_signalled(cand.market_id, now)
            yield self._open(cand)

    def _mark_signalled(self, market_id: str, now: float) -> None:
        """Hold a market back from re-signalling for the cooldown.

        Set on emit rather than on outcome, because the algorithm never hears
        what the runner did with an intent. It does not need to: a fill
        becomes a position and is deduped by `held_ids` forever, so the
        cooldown only ever governs the rejected case.
        """
        if len(self._signalled) > SIGNALLED_CACHE_MAX:
            self._signalled = {
                m: exp for m, exp in self._signalled.items() if exp > now
            }
        self._signalled[market_id] = now + self.params.resignal_cooldown_seconds

    # ── Discovery ────────────────────────────────────────────────────────────

    # ── Diversification bookkeeping ──────────────────────────────────────────

    def _held_buckets(self, held) -> tuple[Counter, Counter]:
        """Event and category counts across open positions.

        The cache is primed whenever we open, so the Gamma lookups here only
        happen for positions that predate this process — after a restart, at
        most `max_concurrent_positions` of them, once.
        """
        events: Counter = Counter()
        categories: Counter = Counter()
        for pos in held:
            event_id, category = self._bucket_of(pos.market_id)
            # An unresolvable bucket must not read as "unconstrained": count
            # it under the market id so it still consumes an event slot.
            events[event_id or pos.market_id] += 1
            if category:
                categories[category] += 1
        return events, categories

    def _bucket_of(self, market_id: str) -> tuple[str, Optional[str]]:
        if market_id in self._buckets:
            return self._buckets[market_id]
        market = self._market_data.market(market_id)
        if market is None:
            return "", None          # not cached — the next poll retries
        labels = self._market_data.market_labels(market)
        bucket = (
            screen.event_id(market),
            labels.split(",")[0] if labels else None,
        )
        self._remember_bucket(market_id, bucket)
        return bucket

    def _remember_bucket(self, market_id: str, bucket: tuple) -> None:
        """Cache a market's (event, category), bounded.

        Insertion-ordered, so this drops the least recently added fifth when
        full. Evicting is safe rather than merely cheap: a miss re-fetches
        from Gamma, so the cache is an optimisation and never a source of
        truth.
        """
        if len(self._buckets) >= BUCKET_CACHE_MAX:
            for stale in list(self._buckets)[: BUCKET_CACHE_MAX // 5]:
                del self._buckets[stale]
        self._buckets[market_id] = bucket

    # ── Entry ────────────────────────────────────────────────────────────────

    def _open(self, cand: screen.Candidate) -> OpenIntent:
        p = self.params
        # Hours, not days: every market this strategy touches resolves inside
        # one, so days round every horizon to "0.0d". A negative horizon is
        # the post-whistle case and reads as time elapsed, not time remaining.
        hours = cand.days * 24
        horizon = (
            f"ended {-hours * 60:.0f}m ago" if hours < 0
            else f"resolves in {hours:.1f}h" if cand.days < 1
            else f"resolves in {cand.days:.1f}d"
        )
        # The A/B's only divergence, and it happens after every gate and the
        # ranking have run on the favourite — so both arms buy into exactly
        # the same markets at the same moment, on opposite sides.
        if p.buy_underdog:
            asset_id, outcome, price = (
                cand.under_asset_id, cand.under_outcome, cand.under_ask,
            )
            tag = "UNDERDOG"
        else:
            asset_id, outcome, price = cand.asset_id, cand.outcome, cand.ask
            tag = "CARRY"

        logger.info(
            "[%s] %s: %s @ %.3f, %s | %s",
            p.name, tag, outcome or "—", price, horizon, cand.question[:52],
        )
        return OpenIntent(
            market_id=cand.market_id,
            asset_id=asset_id,
            usdc_amount=p.bet_size_usdc,
            signal_price=price,
            question=cand.question,
            outcome=outcome,
            signal_id=f"carry:{asset_id}:{self._poll_count}",
            reason=(
                f"{'underdog' if p.buy_underdog else 'ask'} {price:.3f} "
                f"({(1 - price) / price:+.1%} if right), {horizon}"
            ),
            # Raw observables only — the derived score belongs in the
            # analysis, not in the training row.
            features={
                # Always the favourite's book, on both arms: it is the shared
                # observable the two sides are judged against.
                "ask": cand.ask,
                "bid": cand.bid,
                "side": "underdog" if p.buy_underdog else "favourite",
                "entry_price": price,
                "days_to_resolution": cand.days,
                "annualized": cand.annualized,
                "liquidity_clob": cand.liquidity,
                # Splits post-whistle entries from pre-whistle ones in the
                # P&L later, so the grace window can be judged on its own
                # results rather than blended into the strategy's.
                "hours_past_end": max(0.0, -cand.days * 24),
                "market_category": cand.category,
                "event_id": cand.event_id,
                "market_end_ts": cand.end_ts,
            },
        )

    # ── Exit: hold to resolution ─────────────────────────────────────────────

    def _settle_sweep(self) -> Iterator[SettleIntent]:
        yield from settlement.sweep_resolved(
            self._ledger, self._market_data, self._paper,
            self.params.name, self._poll_count,
        )

    def _note_lows(self, asks: dict[str, float], held_ids: set[str]) -> None:
        """Log each new low on a held position.

        No stop-loss in v1 by design, so this exists purely to answer whether
        one is justified: if positions that fall below ~0.5 essentially never
        recover, a catastrophic stop becomes a data-backed decision.

        Reads the shared scan's price map rather than its rows: a held
        position has usually left the band by the time it is worth watching,
        so it is not among the rows the prefilter keeps — but its price is.

        ponytail: in memory and only for markets the scan happens to return —
        a position that drops out of the volume window, or one held while we
        are at max positions, stops being sampled. Persist to `signals` if
        the drawdown question ever needs to be answered precisely.
        """
        # Drop marks for anything no longer held. A settled position's low
        # water is not evidence, and without this the dict grows for every
        # market the strategy has ever been in.
        for gone in [m for m in self._low_water if m not in held_ids]:
            del self._low_water[gone]

        for market_id in held_ids:
            ask = asks.get(market_id)
            if ask is None or ask >= self._low_water.get(market_id, 1.01):
                continue
            self._low_water[market_id] = ask
            position = self._ledger.get(market_id, self._paper)
            logger.info(
                "[%s] New low %.3f (entry %.3f) | %s",
                self.params.name, ask,
                position.avg_price if position else 0.0,
                (position.question if position else market_id)[:55],
            )
