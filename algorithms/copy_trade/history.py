"""Reconstruct a wallet's resolved bets from what the public APIs still hold.

Pure functions over two raw payloads — no network, no DB — so the join can be
tested against saved fixtures.

Every bet is anchored to a BUY in the `/activity` window. `/positions` is used
only as a lookup — "did this market resolve, and which way" — never as an
independent source of bets. That distinction is the whole design:

  * `/positions` keeps a resolved position only until it is redeemed. Winners
    get claimed and vanish; losers have nothing to claim and sit forever.
    Measured on real wallets, 100% of `redeemable` rows were losses (227/227,
    498/498). Treat it as a source and every wallet looks like it never won.
  * `/activity` REDEEM rows are the mirror image — they exist only for
    positions that paid out, so they are all winners.

Reading them as two sources also puts wins and losses on **different clocks**:
a REDEEM is stamped when the wallet got round to claiming, often long after
resolution and usually in bursts, while a loss can only be dated by its
market's end. Any attempt to reconcile that by time window overcorrects — one
pass produced wallets at 100-0 and 1266-1. Anchoring both outcomes to the BUY
that opened the bet keeps a single clock and needs no windowing at all.

A bet still open, or sold off before it resolved, appears in neither branch,
which is correct — an exit is a trade, not a verdict. How often a wallet ends
a bet that way is itself the signal `copyability_score` carries.
"""

from bot.polymarket.api import market_end_ts

from .ranker import ResolvedBet


def _key(row: dict) -> tuple:
    return (str(row.get("conditionId") or ""), row.get("outcomeIndex"))


def _cost_basis(activity: list[dict]) -> dict[tuple, tuple[float, float]]:
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
        held, spent = totals.get(_key(row), (0.0, 0.0))
        totals[_key(row)] = (held + shares, spent + paid)
    return totals


def _redemptions(activity: list[dict]) -> dict[tuple, int]:
    """Bets the wallet claimed a payout on → the earliest claim timestamp."""
    out: dict[tuple, int] = {}
    for row in activity:
        if row.get("type") != "REDEEM" or float(row.get("usdcSize") or 0) <= 0:
            continue
        ts = int(row.get("timestamp") or 0)
        # A partial redemption emits several rows for one bet.
        out[_key(row)] = min(out.get(_key(row), ts), ts)
    return out


def _verdicts(positions: list[dict]) -> dict[tuple, tuple[float, int]]:
    """Unredeemed but settled positions → (outcome, resolution time)."""
    out: dict[tuple, tuple[float, int]] = {}
    for row in positions:
        if not row.get("redeemable"):
            continue
        end_ts = market_end_ts(row)
        if end_ts is None:
            continue
        # A resolved market prices at 0 or 1; anything unclaimed near 1 is a
        # winner the wallet simply has not got round to collecting.
        outcome = 1.0 if float(row.get("curPrice") or 0) > 0.5 else 0.0
        out[_key(row)] = (outcome, int(end_ts))
    return out


def _still_open(positions: list[dict]) -> set[tuple]:
    """Bets whose market has not resolved — pending, not concluded."""
    return {_key(row) for row in positions if not row.get("redeemable")}


def resolved_bets_from(
    wallet: str, activity: list[dict], positions: list[dict],
) -> list[ResolvedBet]:
    """Every bet of `wallet`'s, in the activity window, whose outcome is known.

    Each bet carries the wallet's **copyability score**: the share of its
    *concluded* bets that concluded at resolution rather than by selling out.
    That is the correction for this reconstruction's built-in flattery — only
    bets held to resolution get a verdict, so a wallet that cuts its losers
    early shows a win rate that no one mirroring it could reproduce. Open
    positions are excluded from the ratio entirely: holding a live bet is not
    an exit, and counting it as one would punish anyone with a book.
    """
    basis = _cost_basis(activity)
    redeemed = _redemptions(activity)
    settled = _verdicts(positions)
    pending = _still_open(positions)

    graded: list[tuple[float, float, int]] = []
    exited = 0
    for key, (shares, paid) in basis.items():
        entry = paid / shares if shares > 0 else 0.0
        if not 0 < entry < 1:
            continue
        if key in redeemed:
            graded.append((entry, 1.0, redeemed[key]))
        elif key in settled:
            outcome, resolved_at = settled[key]
            graded.append((entry, outcome, resolved_at))
        elif key in pending:
            continue          # market still live — no verdict either way yet
        else:
            exited += 1       # sold out before it resolved

    concluded = len(graded) + exited
    score = len(graded) / concluded if concluded else 0.0
    return [ResolvedBet(wallet=wallet.lower(), entry_price=entry, outcome=outcome,
                        resolved_at=resolved_at, copyability_score=score)
            for entry, outcome, resolved_at in graded]
