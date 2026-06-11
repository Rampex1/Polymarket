"""
Kelly-criterion math for binary markets — pure functions, no I/O.

For a binary market share priced at `q` (win pays exactly 1):
    net odds        b  = (1 − q) / q
    Kelly fraction  f* = (p(b+1) − 1) / b  =  (p − q) / (1 − q)
    inverse         p  = q + f·(1 − q)

Two consumers, in order of arrival:
  1. Offline analysis of logged signal features — `implied_belief` turns an
     observed bettor's bankroll fraction into a *lower bound* on their
     subjective probability (rational bettors use fractional Kelly, so the
     true belief is at least this).
  2. Future confidence-weighted sizing — `stake` converts our own edge
     estimate into a fractional-Kelly bet, capped. Not wired into any
     algorithm yet; flat sizing stays until the paper data justifies more.
"""

from typing import Optional


def _check_price(q: float) -> None:
    if not 0.0 < q < 1.0:
        raise ValueError(f"market price must be in (0, 1), got {q}")


def kelly_fraction(p: float, q: float) -> float:
    """Optimal bankroll fraction for belief `p` at market price `q`.

    Clamped to [0, 1]: no edge (p ≤ q) → 0, certainty → full bankroll.
    """
    _check_price(q)
    return min(1.0, max(0.0, (p - q) / (1.0 - q)))


def implied_belief(bet_fraction: float, q: float) -> float:
    """Subjective probability implied by betting `bet_fraction` of bankroll
    at price `q`, assuming at-most-full-Kelly behavior. A lower bound on the
    bettor's true belief; clamped to [q, 1]."""
    _check_price(q)
    return min(1.0, max(q, q + bet_fraction * (1.0 - q)))


def edge(p: float, q: float) -> float:
    """Estimated probability edge over the market price (may be negative)."""
    return p - q


def stake(
    p: float,
    q: float,
    bankroll: float,
    kelly_scale: float = 0.25,
    cap: Optional[float] = None,
) -> float:
    """Fractional-Kelly stake in USDC. Never negative; `cap` is an absolute
    ceiling on top of the scaled Kelly amount."""
    amount = kelly_fraction(p, q) * max(0.0, bankroll) * kelly_scale
    if cap is not None:
        amount = min(amount, cap)
    return max(0.0, amount)
