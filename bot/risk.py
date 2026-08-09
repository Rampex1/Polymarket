"""
risk.py

Pre-trade gate for BUYs, using each algorithm's own caps.
Sells and redeems always pass — blocking one would trap us in a loser.
"""

from .domain.records import Trade
from .ledger import Ledger


class RiskManager:
    """Per-algorithm risk gate.

    Takes the algorithm's `params` object directly so each strategy can have
    its own caps without sharing a global pool with sibling algorithms.
    """

    def __init__(self, ledger: Ledger, params) -> None:
        self.ledger = ledger
        self.params = params
        # Markets where a BUY has failed this session. Cleared on restart.
        # Prevents retry loops and Discord spam after any unrecoverable failure.
        self._buy_failed_markets: set[str] = set()

    def suspend_market(self, market_id: str) -> None:
        self._buy_failed_markets.add(market_id)

    def is_suspended(self, market_id: str) -> bool:
        return market_id in self._buy_failed_markets

    def check(
        self,
        trade: Trade,
        scaled_usdc: float,
        paper: bool = False,
    ) -> tuple[bool, str]:
        """Return (approved, reason).

        Sells and redeems unconditionally pass — they reduce exposure and the
        daily loss limit must not lock the bot into a losing position.
        """
        if trade.action != "BUY":
            return True, ""

        if trade.market_id in self._buy_failed_markets:
            return False, "buy previously failed (suspended until restart)"

        ok, reason = self._check_min_order(scaled_usdc)
        if not ok:
            return False, reason

        ok, reason = self._check_daily_loss(paper)
        if not ok:
            return False, reason

        ok, reason = self._check_position_size(trade, scaled_usdc, paper)
        if not ok:
            return False, reason

        ok, reason = self._check_total_exposure(scaled_usdc, paper)
        if not ok:
            return False, reason

        ok, reason = self._check_paper_balance(scaled_usdc, paper)
        if not ok:
            return False, reason

        return True, ""

    def _check_min_order(self, scaled_usdc: float) -> tuple[bool, str]:
        if scaled_usdc < self.params.min_order_size_usdc:
            return (
                False,
                f"Scaled size ${scaled_usdc:.2f} below min "
                f"${self.params.min_order_size_usdc:.2f}",
            )
        return True, ""

    def _check_daily_loss(self, paper: bool) -> tuple[bool, str]:
        pnl = self.ledger.today_pnl_usdc(paper=paper)
        if pnl < -self.params.daily_loss_limit_usdc:
            return (
                False,
                f"Daily loss limit hit (${pnl:.2f} < "
                f"-${self.params.daily_loss_limit_usdc})",
            )
        return True, ""

    def _check_position_size(
        self, trade: Trade, scaled_usdc: float, paper: bool
    ) -> tuple[bool, str]:
        position = self.ledger.get(trade.market_id, paper)
        current = position.total_cost_usdc if position else 0.0
        if current + scaled_usdc > self.params.max_position_size_usdc:
            return (
                False,
                f"Position size ${current + scaled_usdc:.2f} would exceed "
                f"max ${self.params.max_position_size_usdc}",
            )
        return True, ""

    def _check_total_exposure(
        self, scaled_usdc: float, paper: bool
    ) -> tuple[bool, str]:
        total = self.ledger.total_exposure_usdc(paper)
        if total + scaled_usdc > self.params.max_total_exposure_usdc:
            return (
                False,
                f"Total exposure ${total + scaled_usdc:.2f} would exceed "
                f"max ${self.params.max_total_exposure_usdc}",
            )
        return True, ""

    def _check_paper_balance(
        self, scaled_usdc: float, paper: bool
    ) -> tuple[bool, str]:
        # Live mode delegates balance enforcement to the exchange; only paper
        # needs manual gating.
        if not paper:
            return True, ""
        balance = self.ledger.paper_balance()
        if scaled_usdc > balance:
            return False, f"Insufficient paper balance (${balance:.2f} available)"
        return True, ""
