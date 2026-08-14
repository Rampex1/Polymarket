"""
Pure screening: Gamma market rows → ranked, diversified candidates.

No I/O and no clock of its own — every input is passed in, so the whole
funnel can be swept offline against archived rows without a network or a
database. The algorithm module owns the fetching, caching, and logging.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Optional


# Whole word: must not match the "Winner" in an esports "Game 2 Winner".
_WIN_RE = re.compile(r"\bwin\b", re.I)


@dataclass(frozen=True)
class Candidate:
    """One tradeable carry, with the raw observables behind the decision."""

    market_id: str
    asset_id: str
    question: str
    outcome: str
    ask: float
    bid: float
    liquidity: float
    end_ts: float
    days: float
    annualized: float
    event_id: str
    category: Optional[str] = None


def evaluate(market: dict, end_ts: Optional[float], now: float, p) -> "Candidate | str":
    """Price, liquidity, and time-value gates for one market row.

    Returns a `Candidate`, or a short string naming the gate that rejected
    it — the caller tallies those into a per-poll funnel, which is the only
    way to tell "nothing qualified" from "the scan is broken". The band is
    thin (measured: 29 of 2,100 open markets), so that distinction matters.
    """
    market_id = str(market.get("conditionId") or "")
    if not market_id:
        return "no condition id"

    # Token 0 is the side Gamma's bestAsk/bestBid quote. Buying the ask means
    # buying that token, so the two must be read together or we would size a
    # trade off one side's price and take delivery of the other.
    asset_id, outcome = _token0(market)
    if not asset_id:
        return "no token id"

    ask, bid = to_float(market.get("bestAsk")), to_float(market.get("bestBid"))
    # The ask, never lastTradePrice or the mid: the last trade may be stale
    # and the mid is unfillable. It is what we would actually pay.
    if ask is None or not (p.min_ask <= ask <= p.max_ask):
        return "out of band"

    # In-play only. 0.95 before kickoff and 0.95 with the game underway are
    # not the same number: the first is a forecast that a thing will happen,
    # the second is a scoreboard that has largely already decided it. This
    # strategy is paid for waiting on a settled outcome, not for being right
    # about an unsettled one, and only the second is that.
    if p.require_in_play:
        start_ts = game_start_ts(market)
        if start_ts is None:
            # No start time means it is not a game at all (weather, tweet
            # counts, index levels). Those never become "live" — they just
            # expire — so there is nothing to verify and we fail closed.
            return "not a live event"
        if start_ts > now:
            return "pregame"

    # Moneyline only. The hypothesis being tested is that 95% on "who wins"
    # is a steadier 95% than on any other market shape: it is read off a
    # scoreline and a clock, whereas an O/U 0.5 at 95% is still a forecast
    # that a goal will arrive, and a prop can turn on one incident.
    if p.require_winner_market and not is_winner_market(market):
        return "not a winner market"

    if bid is None or ask - bid > p.max_ask_spread:
        return "spread"

    liquidity = to_float(market.get("liquidityClob")) or 0.0
    if liquidity < p.min_market_liquidity_usdc:
        return "illiquid"

    # Unknown end date fails closed — an un-priceable wait cannot clear a
    # time-value hurdle.
    if end_ts is None:
        return "no end date"
    hours = (end_ts - now) / 3_600.0
    # Past the end date splits into two populations that look identical to a
    # sign test and are nothing alike. A market whose whistle went minutes ago
    # is the purest form of this trade — the result is known and only the
    # oracle is pending — and measured post-end books stay live for ten to
    # forty minutes at 0.95-0.99. A market whose end date passed *months* ago
    # is a fossil nobody ever resolved (106 of 500 rows), and its quote is an
    # artifact. `max_hours_past_end` is the line between them.
    if hours < -p.max_hours_past_end:
        return "long past end"
    # The floor is about not buying something that settles before we are
    # filled; it has no meaning once the event is already over.
    if 0 <= hours < p.min_hours_to_resolution:
        return "resolves too soon"
    days = hours / 24.0
    if days > p.max_days_to_resolution:
        return "resolves too far out"

    annualized = (1.0 - ask) / ask * 365.0 / max(days, 1.0)
    if annualized < p.min_annualized_return:
        return "not worth the wait"

    return Candidate(
        market_id=market_id,
        asset_id=asset_id,
        question=str(market.get("question") or ""),
        outcome=outcome,
        ask=ask,
        bid=bid,
        liquidity=liquidity,
        end_ts=end_ts,
        days=days,
        annualized=annualized,
        event_id=event_id(market),
    )


def category_ok(labels: str, p) -> bool:
    """Gamma category/tag screen. `labels` is the lowercased comma-joined
    string from the gateway; empty means Gamma carries no labels.

    Sports is required rather than excluded here, and that inversion is the
    point: everywhere else efficient pricing kills a forecasting edge, but
    this strategy is paid for waiting, not for knowing better. Unlabelled
    fails closed under `require_sports` — we cannot confirm the universe.
    """
    if p.require_sports and "sports" not in labels:
        return False
    return not (labels and any(exc in labels for exc in p.exclude_categories))


def select(
    candidates: Iterable[Candidate],
    held_events: Counter,
    held_categories: Counter,
    slots: int,
    p,
) -> list[Candidate]:
    """Rank by annualised return and take what the diversification caps allow.

    Return per unit of capital-time is the whole point, so a 2% trade
    resolving in five days beats a 5% trade resolving in ninety.

    The caps are what stop twenty legs of one election reading as twenty
    independent positions, so they apply to this poll's own selections too,
    not just to what is already held.
    """
    events, categories = Counter(held_events), Counter(held_categories)
    picked: list[Candidate] = []
    for cand in sorted(candidates, key=lambda c: c.annualized, reverse=True):
        if len(picked) >= slots:
            break
        if cand.event_id and events[cand.event_id] >= p.max_positions_per_event:
            continue
        if cand.category and categories[cand.category] >= p.max_positions_per_category:
            continue
        events[cand.event_id] += 1
        categories[cand.category] += 1
        picked.append(cand)
    return picked


def with_category(cand: Candidate, labels: str) -> Candidate:
    """Attach the primary Gamma label; None when Gamma carries none."""
    return replace(cand, category=labels.split(",")[0] if labels else None)


# ── Row parsing ──────────────────────────────────────────────────────────────

def to_float(raw) -> Optional[float]:
    """Gamma returns numbers as floats or as strings depending on the field."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _token0(market: dict) -> tuple[str, str]:
    """(token id, outcome label) for the side Gamma quotes. ('', '') if absent."""
    tokens = _json_list(market.get("clobTokenIds"))
    if not tokens:
        return "", ""
    outcomes = _json_list(market.get("outcomes"))
    return str(tokens[0]), str(outcomes[0]) if outcomes else "Yes"


def _json_list(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def is_winner_market(market: dict) -> bool:
    """A moneyline — "Will <team> win on <date>?" — and nothing else.

    Two conditions, because either alone lets the wrong thing through.
    Yes/No outcomes exclude totals (which quote Over/Under) and esports
    head-to-heads (which quote team names), but 682 of 751 live Yes/No sports
    markets are props: draws, both-teams-to-score, exact scorelines, tweet
    counts. Requiring "win" as a whole word cuts those, and the word boundary
    matters — it must not match the "Winner" in an esports "Game 2 Winner".
    """
    outcomes = [str(o).strip().lower() for o in _json_list(market.get("outcomes"))]
    if outcomes != ["yes", "no"]:
        return False
    return bool(_WIN_RE.search(market.get("question") or ""))


def game_start_ts(market: dict) -> Optional[float]:
    """Kick-off as a unix timestamp, or None when Gamma has no `gameStartTime`.

    Deliberately does NOT fall back to `startDate`: that is when the *market*
    opened, which every market has, so a fallback would report every row as
    already started and silently turn the in-play gate off.
    """
    raw = market.get("gameStartTime")
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def event_id(market: dict) -> str:
    events = market.get("events") or []
    return str(events[0].get("id") or "") if events else ""
