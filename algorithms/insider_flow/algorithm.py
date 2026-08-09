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
  6. Gamma category/tags not matching `exclude_categories` — the
     authoritative sports screen (title patterns miss "Will <team> win on
     <date>?" formats). Cached per market. A market with no category data
     fails open (noise filter; title patterns already passed), but a failed
     Gamma lookup fails CLOSED — gate 7 can't run without the market row.
  7. time-value gate: the market must resolve within
     `max_days_to_resolution`, and the win-case return annualized over the
     wait must beat `min_annualized_return`. Insider info is about imminent
     events, and a +5% payoff a year out loses to an index fund. Unknown
     end date = un-priceable wait → fail closed.
  8. ONLY THEN the wallet call: freshness via one /activity page — young
     account, few prior trades, and verifiable. An unverifiable wallet
     (stats fetch failed) is NOT copied: fail closed.

Survivors do NOT trade immediately: they buffer for
`buffer_window_seconds`, the window's cohort is ranked by a conviction
score (bet size, wallet youth, history thinness), and only the top
`buffer_top_n` become OpenIntents (top-up to `bet_size_usdc`) — one whale
bet can be a random degenerate; the cohort's best-scored signals are where
conviction concentrates. Exits are v1 hold-to-resolution: every
`settle_check_every` polls, open positions are checked against Gamma and
resolved markets yield SettleIntents. Mirroring the source wallet's SELL
is a deliberate non-goal until paper data shows whether these wallets dump
before resolution.
"""

import logging
import math
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

from bot import fetcher
from bot.algorithm import Algorithm
from bot.domain.intents import Intent, OpenIntent, SettleIntent
from bot.domain.params import Mode
from bot.integrations.polymarket import DEFAULT_MARKET_DATA, MarketDataGateway
from bot.domain.records import GlobalTrade

from .params import InsiderFlowParams

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
    """Copy large first bets from fresh wallets at long odds."""

    def __init__(
        self,
        params: InsiderFlowParams,
        market_data: Optional[MarketDataGateway] = None,
    ) -> None:
        """`params` is required — there are no schema defaults to fall back on."""
        self.params = params

        self._market_data = market_data or DEFAULT_MARKET_DATA

        self._tracker = None        # PositionTracker, set in setup()
        self._paper: bool = self.params.mode == Mode.PAPER
        self._seen = fetcher.SeenRing(SEEN_IDS_MAX)
        self._poll_count = 0
        # wallet → (expires_at, stats-dict-if-fresh-else-None).
        # Failed lookups are never cached.
        self._wallet_verdicts: dict[str, tuple[float, Optional[dict]]] = {}
        # market_id → {"cats": str, "end_ts": float|None}. Categories and end
        # dates don't change, so no TTL. Lookup failures are not cached.
        self._market_info_cache: dict[str, dict] = {}
        # Candidate buffer (see _flush_buffer) and the window's start time.
        self._buffer: list[dict] = []
        self._buffer_started: float = 0.0

    @property
    def display_name(self) -> str:
        return f"{self.name} → fresh-wallet flow"

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def setup(self, tracker) -> None:
        self._tracker = tracker
        self._paper = self.params.mode == Mode.PAPER

        # Seed the dedupe ring with the current firehose tail so a restart
        # never replays trades that already happened.
        for row in self._market_data.global_trades(
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
        p = self.params

        rows = self._market_data.global_trades(
            p.min_cash_size_usdc, p.firehose_limit,
        )
        new_rows = [r for r in rows if self._key(r) not in self._seen]
        for row in sorted(new_rows, key=lambda r: r.timestamp):
            # Mark immediately — a rejected row stays rejected and must not
            # re-run its (possibly network-priced) checks every tick.
            self._seen.mark(self._key(row))
            candidate = self._screen(row)
            if candidate is None:
                continue
            if p.buffer_window_seconds <= 0:
                yield from self._emit(candidate, cohort_size=1)
            else:
                if not self._buffer:
                    self._buffer_started = time.time()
                self._buffer.append(candidate)
                logger.info(
                    "[%s] Buffered candidate %d (score %.2f): %s",
                    p.name, len(self._buffer), candidate["score"],
                    row.title[:50],
                )

        # Flush when the window has run its course, or early on a burst.
        if self._buffer and (
            time.time() - self._buffer_started >= p.buffer_window_seconds
            or len(self._buffer) >= p.buffer_max
        ):
            yield from self._flush_buffer()

        if self._poll_count % p.settle_check_every == 0:
            yield from self._settle_sweep()

    # ── Filter chain → candidate ─────────────────────────────────────────────

    def _screen(self, row: GlobalTrade) -> Optional[dict]:
        """Run the full filter chain. Returns a scored candidate dict for
        rows that pass, None otherwise."""
        p = self.params

        # Cheap, local gates first.
        if row.side != "BUY":
            return None
        if not (0 < row.price <= p.max_entry_odds):
            return None
        if row.cash_usdc < p.min_cash_size_usdc:
            return None
        if any(pat in row.title for pat in p.exclude_title_patterns):
            logger.debug("[%s] Excluded by title pattern: %s",
                         p.name, row.title[:50])
            return None

        # Position gate before any network call — it's free and skips the
        # common case of repeat buys in a market we already copied.
        if self._top_up_amount(row.market_id) < p.min_order_size_usdc:
            logger.info(
                "[%s] Already at bet size for market, skipping: %s",
                p.name, row.title[:50],
            )
            return None

        # Market gates — one cached Gamma lookup feeds both. Checked before
        # wallet vetting: the per-market cache hits far more often than the
        # per-wallet one (hot markets repeat in the firehose). The lookup
        # failing fails CLOSED: the time-value gate below cannot run without
        # it, and with a thin bankroll an unknowable resolution horizon is
        # not a bet — the market's next trade retries (failures not cached).
        info = self._market_info(row.market_id)
        if info is None:
            logger.warning(
                "[%s] No market info from Gamma — skipping (fail closed): %s",
                p.name, row.title[:50],
            )
            return None

        # Category gate — authoritative sports screen. Title patterns miss
        # formats like "Will <team> win on <date>?"; Gamma tags don't.
        # Absent category data fails open (it's a noise filter and the
        # title patterns above already passed).
        cats = info["cats"]
        if cats and any(exc in cats for exc in p.exclude_categories):
            logger.info("[%s] Excluded by category (%s): %s",
                        p.name, cats.split(",")[0], row.title[:50])
            return None

        # Time-value gate — capital is thin and insiders act on *imminent*
        # events. A far-out market both locks the bankroll and dilutes the
        # win-case return below what an index fund pays for the same wait.
        if not self._worth_the_wait(row, info):
            return None

        # The one wallet-priced gate, last.
        stats = self._vet_wallet(row.wallet)
        if stats is None:
            return None

        return {
            "row": row,
            "stats": stats,
            "cats": cats,
            "end_ts": info["end_ts"],
            "score": self._score(row, stats),
        }

    def _worth_the_wait(self, row: GlobalTrade, info: dict) -> bool:
        """Time-value-of-money gate. The horizon must be short (insider info
        is about imminent events) and the win-case return, linearly
        annualized over the time to resolution, must clear the hurdle —
        +5% resolving tomorrow is a trade, +5% locked for a year loses to
        the S&P. Unknown end date fails closed: can't price the wait."""
        p = self.params
        end_ts = info["end_ts"]
        if end_ts is None:
            logger.warning(
                "[%s] Market has no parseable end date — skipping "
                "(fail closed): %s", p.name, row.title[:50],
            )
            return False

        days = max(0.0, (end_ts - time.time()) / 86_400)
        if days > p.max_days_to_resolution:
            logger.info(
                "[%s] Resolves too far out (%.0fd > %.0fd cap): %s",
                p.name, days, p.max_days_to_resolution, row.title[:50],
            )
            return False

        win_return = (1.0 - row.price) / row.price
        annualized = win_return * 365.0 / max(days, 1.0)
        if annualized < p.min_annualized_return:
            logger.info(
                "[%s] Win-case return not worth the wait "
                "(%.0f%%/yr < %.0f%%/yr hurdle, %.0fd horizon): %s",
                p.name, annualized * 100, p.min_annualized_return * 100,
                days, row.title[:50],
            )
            return False
        return True

    def _top_up_amount(self, market_id: str) -> float:
        """USDC needed to bring our position in this market to bet size."""
        position = self._tracker.get(market_id, self._paper)
        current_cost = position.total_cost_usdc if position else 0.0
        return round(self.params.bet_size_usdc - current_cost, 8)

    def _score(self, row: GlobalTrade, stats: dict) -> float:
        """Conviction heuristic for ranking buffered candidates, ~[0, 3]:
        bet size (log-scaled), wallet youth, and history thinness.

        Hand-tuned v1 — to be replaced by a model fitted on the logged
        signal features once enough labeled outcomes exist.
        """
        p = self.params
        cash_score = min(1.0, math.log10(
            max(1.0, row.cash_usdc / p.min_cash_size_usdc)))
        oldest = stats.get("oldest_ts")
        age_days = ((time.time() - oldest) / 86_400) if oldest else 0.0
        age_score = max(0.0, 1.0 - age_days / p.max_wallet_age_days)
        history_score = max(0.0, 1.0 - (
            stats.get("trade_count") or 0) / max(1, p.max_prior_trades))
        return cash_score + age_score + history_score

    # ── Buffer → best candidates → intents ──────────────────────────────────

    def _flush_buffer(self) -> Iterator[OpenIntent]:
        """Rank the window's candidates and emit only the strongest few.
        One whale bet can be a random degenerate; the cohort's best-scored
        signals are where the conviction concentrates."""
        ranked = sorted(self._buffer, key=lambda c: c["score"], reverse=True)
        cohort = len(ranked)
        selected = ranked[: max(1, self.params.buffer_top_n)]
        for cand in ranked[len(selected):]:
            logger.info(
                "[%s] Dropped candidate (score %.2f, cohort %d): %s",
                self.params.name, cand["score"], cohort,
                cand["row"].title[:50],
            )
        self._buffer = []
        for cand in selected:
            yield from self._emit(cand, cohort_size=cohort)

    def _emit(self, candidate: dict, cohort_size: int) -> Iterator[OpenIntent]:
        row, stats, cats = candidate["row"], candidate["stats"], candidate["cats"]
        p = self.params

        # Re-check the position gate — another candidate in the same flush
        # (or an earlier window) may have filled this market meanwhile.
        scaled = self._top_up_amount(row.market_id)
        if scaled < p.min_order_size_usdc:
            return

        wallet_short = f"{row.wallet[:6]}…{row.wallet[-4:]}"
        end_ts = candidate["end_ts"]
        days = max(0.0, (end_ts - time.time()) / 86_400)
        annualized = (1.0 - row.price) / row.price * 365.0 / max(days, 1.0)
        logger.info(
            "[%s] SUSPICIOUS FLOW: fresh wallet %s bet $%.0f @ %.2f "
            "(score %.2f, cohort %d) | %s",
            p.name, wallet_short, row.cash_usdc, row.price,
            candidate["score"], cohort_size, row.title[:55],
        )
        features = self._features_for(row, stats, cats, end_ts)
        features["buffer_score"] = candidate["score"]
        features["buffer_cohort_size"] = cohort_size
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
                f"${row.cash_usdc:,.0f} @ {row.price:.2f} "
                f"(score {candidate['score']:.1f}, cohort {cohort_size}; "
                f"resolves in {days:.0f}d, ~{annualized:+.0%}/yr if right)"
            ),
            features=features,
        )

    def _market_info(self, market_id: str) -> Optional[dict]:
        """Gate inputs from one Gamma lookup: lowercased comma-joined
        category/tag labels and the market end date as a unix timestamp.

        Cached forever per market (neither changes). Lookup failure → None,
        NOT cached — the next trade in the market retries.
        """
        if market_id in self._market_info_cache:
            return self._market_info_cache[market_id]

        market = self._market_data.market(market_id)
        if market is None:
            return None

        labels = []
        if market.get("category"):
            labels.append(str(market["category"]))
        for event in market.get("events") or []:
            for tag in event.get("tags") or []:
                for key in ("label", "slug"):
                    if tag.get(key):
                        labels.append(str(tag[key]))

        info = {
            "cats": ",".join(dict.fromkeys(l.lower() for l in labels)),
            "end_ts": self._parse_end_ts(market),
        }
        if len(self._market_info_cache) > 2000:
            self._market_info_cache.clear()
        self._market_info_cache[market_id] = info
        return info

    @staticmethod
    def _parse_end_ts(market: dict) -> Optional[float]:
        """Gamma end date (ISO-8601, sometimes date-only `endDateIso`) →
        unix timestamp. Unparseable → None (caller fails closed)."""
        for key in ("endDate", "endDateIso"):
            raw = market.get(key)
            if not raw or not isinstance(raw, str):
                continue
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        return None

    def _features_for(
        self, row: GlobalTrade, stats: dict, cats: Optional[str],
        end_ts: Optional[float],
    ) -> dict:
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
            "portfolio_value_usdc": self._market_data.wallet_value(row.wallet),
            "market_category": cats.split(",")[0] if cats else None,
            "market_end_ts": end_ts,
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
        stats = self._market_data.wallet_stats(wallet)
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
        # Strict finality gate: `closed` alone means trading ended, not that
        # the outcome is determined (UMA dispute window). Settling there
        # would book P&L off a stale book and permanently mislabel this
        # market's signal rows. Undetermined markets just wait for a later
        # sweep — resolution is not time-sensitive.
        for pos in self._tracker.all_open(paper=self._paper):
            market = self._market_data.market(pos.market_id)
            if market and self._market_data.market_outcome_is_final(market):
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
