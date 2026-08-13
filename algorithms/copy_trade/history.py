"""Reconstruct a wallet's resolved bets from what the public APIs still hold.

Pure functions over two raw payloads — no network, no DB — so the join can be
tested against saved fixtures.

The two sources are complementary, and **either one alone is catastrophically
skewed**:

  * `/positions` keeps a resolved position only until it is redeemed. Winners
    get claimed and vanish; losers have nothing to claim and sit there
    forever. Measured on real wallets, 100% of `redeemable` rows were losses
    (227/227 and 498/498). Rank on this alone and every wallet looks like it
    has never won a bet.
  * `/activity` REDEEM rows are the mirror image: they exist only for
    positions that paid out, so they are all winners.

Together they cover both outcomes. A wallet that sold before resolution
appears in neither, which is correct — an exit is a trade, not a verdict.

The two sources also cover different *spans*, which is the subtler trap.
`/positions` accumulates unredeemed losers for the wallet's whole lifetime,
while a capped `/activity` crawl sees only recent history. Measured on one
real wallet: 11 days of wins against 4 months of losses, scoring a −0.42
"edge" that was entirely an artifact of the mismatch. `since_ts` clamps the
loss side to the window the win side actually covers.
"""

from typing import Optional

from bot.polymarket.api import market_end_ts

from .ranker import ResolvedBet


def _entry_cost_basis(activity: list[dict]) -> dict[tuple, tuple[float, float]]:
    """(conditionId, outcomeIndex) → (shares bought, USDC paid).

    `usdcSize` is what actually left the wallet — a little above size × price,
    because it carries the fee. That is the honest basis for an edge number.
    """
    totals: dict[tuple, tuple[float, float]] = {}
    for row in activity:
        if row.get("type") != "TRADE" or row.get("side") != "BUY":
            continue
        shares = float(row.get("size") or 0)
        paid = float(row.get("usdcSize") or 0) or shares * float(row.get("price") or 0)
        if shares <= 0 or paid <= 0:
            continue
        key = (str(row.get("conditionId") or ""), row.get("outcomeIndex"))
        held, spent = totals.get(key, (0.0, 0.0))
        totals[key] = (held + shares, spent + paid)
    return totals


def resolved_bets_from(
    wallet: str, activity: list[dict], positions: list[dict],
    copyability_score: float = 1.0, since_ts: Optional[int] = None,
) -> list[ResolvedBet]:
    """Every bet of `wallet`'s we can still see the outcome of.

    Pass `since_ts` whenever the activity crawl was capped rather than
    exhausted: it is the oldest event actually retrieved, and losses older
    than it are dropped so both outcomes are drawn from one window.
    """
    basis = _entry_cost_basis(activity)
    bets: list[ResolvedBet] = []
    claimed: set[tuple] = set()

    for row in activity:
        if row.get("type") != "REDEEM" or float(row.get("usdcSize") or 0) <= 0:
            continue
        key = (str(row.get("conditionId") or ""), row.get("outcomeIndex"))
        # A partial redemption can emit several rows for one bet.
        if key in claimed:
            continue
        shares, paid = basis.get(key, (0.0, 0.0))
        # The buys that opened this bet fell off the end of the activity
        # window, so there is no entry price to score. Drop it rather than
        # guess — a fabricated basis is worse than a smaller sample.
        if shares <= 0:
            continue
        claimed.add(key)
        entry = paid / shares
        if not 0 < entry < 1:
            continue
        bets.append(ResolvedBet(
            wallet=wallet.lower(), entry_price=entry, outcome=1.0,
            resolved_at=int(row.get("timestamp") or 0),
            copyability_score=copyability_score,
        ))

    for row in positions:
        if not row.get("redeemable"):
            continue
        entry = float(row.get("avgPrice") or 0)
        if not 0 < entry < 1:
            continue
        # A resolved market prices at 0 or 1. Anything unredeemed and still
        # near 1 is a winner the wallet has not claimed yet.
        outcome = 1.0 if float(row.get("curPrice") or 0) > 0.5 else 0.0
        resolved_at = market_end_ts(row)
        if resolved_at is None:
            continue
        if since_ts is not None and resolved_at < since_ts:
            continue
        bets.append(ResolvedBet(
            wallet=wallet.lower(), entry_price=entry, outcome=outcome,
            resolved_at=int(resolved_at), copyability_score=copyability_score,
        ))
    return bets
