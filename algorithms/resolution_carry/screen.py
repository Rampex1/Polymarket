"""
Pure screening: Gamma market rows → ranked, diversified candidates.

No I/O and no clock of its own — every input is passed in, so the whole
funnel can be swept offline against archived rows without a network or a
database. The algorithm module owns the fetching, caching, and logging.
"""

import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import Iterable, Optional


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
    # Past its end date is not "nearly settled", it is settled: trading has
    # effectively stopped, so the quote is a dead book and there is no carry
    # left to earn. Kept separate from the floor below so the funnel says
    # which one fired.
    if hours < 0:
        return "past end date"
    if hours < p.min_hours_to_resolution:
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


def event_id(market: dict) -> str:
    events = market.get("events") or []
    return str(events[0].get("id") or "") if events else ""
