"""Fill records and paper-exchange behavior.

This module is deliberately free of database, notification, and strategy
dependencies.  It is therefore the smallest execution unit to test in
isolation and is shared by the dispatcher and any future backtest runner.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class FillResult:
    """The actual quantity and cost/proceeds resulting from an order."""

    success: bool
    shares: float
    amount_usdc: float
    fill_price: float
    fee_usdc: float = 0.0
    reason: str = ""


def simulate_buy(
    scaled_usdc: float, current_price: Optional[float], paper_fee_bps: float,
) -> FillResult:
    """Simulate a market BUY at the observed price with configured fees."""
    if current_price is None or current_price <= 0:
        return FillResult(False, 0, 0, 0, reason="no current price")
    fee = scaled_usdc * (paper_fee_bps / 10_000.0)
    if scaled_usdc - fee <= 0:
        return FillResult(False, 0, 0, 0, reason="fee exceeds order size")
    return FillResult(
        success=True,
        shares=(scaled_usdc - fee) / current_price,
        amount_usdc=scaled_usdc,
        fill_price=current_price,
        fee_usdc=fee,
    )


def simulate_sell(
    shares: float, current_price: Optional[float], paper_fee_bps: float,
) -> FillResult:
    """Simulate a market SELL at the observed price with configured fees."""
    if current_price is None or current_price <= 0:
        return FillResult(False, 0, 0, 0, reason="no current price")
    gross = shares * current_price
    return FillResult(
        success=True,
        shares=shares,
        amount_usdc=gross,
        fill_price=current_price,
        fee_usdc=gross * (paper_fee_bps / 10_000.0),
    )
