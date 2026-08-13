"""Snapshot consensus — which markets a wallet cohort agrees on.

Pure functions over Data-API `/positions` rows: no network, no DB, no config,
so the thresholds can be swept offline against a saved snapshot. Phase 1
imports `find_consensus` unchanged; only the caller changes.

Consensus is computed on *standing positions*, not on a rolling window of
entry events. That catches a cohort who accumulated the same side days apart,
survives a missed poll (the position is still there next tick), and yields the
exit signal for free — support falling is a cohort walking away.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Holding:
    wallet: str
    asset_id: str
    market_id: str
    outcome: str
    title: str
    cost_usdc: float
    avg_price: float
    current_price: float
    opposite_asset: str
    end_date: str


@dataclass(frozen=True)
class Consensus:
    asset_id: str
    market_id: str
    outcome: str
    title: str
    end_date: str
    wallets: tuple[str, ...]
    opposition: int
    cohort_cost_usdc: float
    avg_entry: float
    current_price: float

    @property
    def support(self) -> int:
        return len(self.wallets)

    @property
    def margin(self) -> int:
        return self.support - self.opposition

    @property
    def drift(self) -> float:
        """How far the price has run past what the cohort paid.

        The edge is in their entry, not in the outcome. A high drift means the
        move already happened and you would be buying their exit liquidity.
        """
        return (self.current_price - self.avg_entry) / self.avg_entry if self.avg_entry > 0 else 0.0


def parse_positions(wallet: str, rows: list[dict]) -> list[Holding]:
    """Data-API position rows → live holdings for one wallet."""
    out = []
    for r in rows:
        # `redeemable` means the market already resolved: history, not a live bet.
        if r.get("redeemable"):
            continue
        cost = float(r.get("initialValue") or 0)
        if cost <= 0 or float(r.get("size") or 0) <= 0:
            continue
        out.append(Holding(
            wallet=wallet.lower(),
            asset_id=str(r.get("asset") or ""),
            market_id=str(r.get("conditionId") or ""),
            outcome=str(r.get("outcome") or ""),
            title=str(r.get("title") or ""),
            # initialValue is cost basis. currentValue would inflate as a
            # position wins, so conviction-weighting on it would mechanically
            # pile into whatever already ran.
            cost_usdc=cost,
            avg_price=float(r.get("avgPrice") or 0),
            current_price=float(r.get("curPrice") or 0),
            opposite_asset=str(r.get("oppositeAsset") or ""),
            end_date=str(r.get("endDate") or ""),
        ))
    return out


def find_consensus(
    holdings: list[Holding], *, min_support: int = 3, min_margin: int = 2,
    min_conviction: float = 0.0, max_price: float = 0.97, min_price: float = 0.05,
) -> list[Consensus]:
    """Markets where enough of the cohort holds the same side.

    Keyed by `asset_id`, which is unique per (market, outcome) — grouping by
    market alone would read five wallets on YES plus five on NO as ten-way
    agreement when it is maximum disagreement.

    `max_price`/`min_price` drop the decided-but-not-yet-resolved at either
    end. A finished match sits at 1.000 with `redeemable` still false and
    would otherwise top the ranking with nothing left to pay out; at the other
    end, a cohort sitting on a position down 98% is a fossil nobody bothered
    to sell, not a live opinion worth copying.

    ponytail: opposition only looks at the direct `oppositeAsset`. In a
    negative-risk event (one market per team) a wallet holding a rival's YES
    is really betting against us and is counted as neutral. Group on `eventId`
    if that starts mattering.
    """
    book: dict[str, float] = {}
    for h in holdings:
        book[h.wallet] = book.get(h.wallet, 0.0) + h.cost_usdc

    # Opposition is counted from the unfiltered snapshot: a wallet betting
    # against the cohort counts even if the stake is too small to be a signal.
    holders_of: dict[str, set[str]] = {}
    for h in holdings:
        holders_of.setdefault(h.asset_id, set()).add(h.wallet)

    by_asset: dict[str, list[Holding]] = {}
    for h in holdings:
        if min_conviction > 0 and book[h.wallet] > 0 and h.cost_usdc / book[h.wallet] < min_conviction:
            continue
        by_asset.setdefault(h.asset_id, []).append(h)

    out = []
    for asset_id, hs in by_asset.items():
        wallets = tuple(sorted({h.wallet for h in hs}))
        # Subtract our own holders: a wallet holding both sides is hedged, not
        # an opponent of itself.
        opposition = len(holders_of.get(hs[0].opposite_asset, set()) - set(wallets))
        if len(wallets) < min_support or len(wallets) - opposition < min_margin:
            continue
        cost = sum(h.cost_usdc for h in hs)
        price = max(h.current_price for h in hs)
        if not (min_price <= price <= max_price):
            continue
        out.append(Consensus(
            asset_id=asset_id,
            market_id=hs[0].market_id,
            outcome=hs[0].outcome,
            title=hs[0].title,
            end_date=hs[0].end_date,
            wallets=wallets,
            opposition=opposition,
            cohort_cost_usdc=cost,
            # Cost-weighted: a $50k conviction bet should move the reference
            # price more than a $500 dart.
            avg_entry=sum(h.avg_price * h.cost_usdc for h in hs) / cost if cost else 0.0,
            current_price=price,
        ))
    return sorted(out, key=lambda c: (c.support, c.margin, c.cohort_cost_usdc), reverse=True)
