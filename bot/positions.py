"""
Phase 4: Position tracking and risk management.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import config
from . import db
from .models import Trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------


@dataclass
class Position:
    market_id: str
    asset_id: str
    question: str
    outcome: str
    shares: float
    avg_price: float
    total_cost_usdc: float
    opened_at: int
    updated_at: int

    @property
    def current_value_usdc(self) -> float:
        return self.shares * self.avg_price

    def __str__(self) -> str:
        return (
            f"{self.outcome} | {self.shares:.2f} shares @ avg {self.avg_price:.3f}"
            f" | cost ${self.total_cost_usdc:.2f} | {self.question[:55]}"
        )


# ---------------------------------------------------------------------------
# Position tracker
# ---------------------------------------------------------------------------


class PositionTracker:
    # ── Paper balance ────────────────────────────────────────────────────────

    def init_paper_balance(self, starting: float) -> None:
        """Seed balance only on first run; subsequent calls are no-ops."""
        db.get().execute(
            "INSERT OR IGNORE INTO paper_account (id, balance) VALUES (1, ?)",
            (starting,),
        )
        db.get().commit()

    def paper_balance(self) -> float:
        row = (
            db.get().execute("SELECT balance FROM paper_account WHERE id=1").fetchone()
        )
        return float(row[0]) if row else 0.0

    def _adjust_paper_balance(self, delta: float) -> None:
        db.get().execute(
            "UPDATE paper_account SET balance = balance + ? WHERE id=1", (delta,)
        )
        db.get().commit()

    # ── Trade recording ──────────────────────────────────────────────────────

    def record_buy(self, trade: Trade, scaled_usdc: float, paper: bool = False) -> None:
        if trade.price <= 0:
            return
        shares = scaled_usdc / trade.price
        conn = db.get()
        existing = self.get(trade.market_id)

        if existing:
            new_shares = existing.shares + shares
            new_cost = existing.total_cost_usdc + scaled_usdc
            new_avg = new_cost / new_shares if new_shares > 0 else 0
            conn.execute(
                """UPDATE positions
                   SET shares=?, avg_price=?, total_cost_usdc=?, updated_at=?,
                       asset_id=?, outcome=?
                   WHERE market_id=?""",
                (
                    new_shares,
                    new_avg,
                    new_cost,
                    int(time.time()),
                    trade.asset_id,
                    trade.outcome,
                    trade.market_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO positions
                   (market_id, asset_id, question, outcome, shares, avg_price,
                    total_cost_usdc, opened_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    trade.market_id,
                    trade.asset_id,
                    trade.question,
                    trade.outcome,
                    shares,
                    trade.price,
                    scaled_usdc,
                    int(time.time()),
                    int(time.time()),
                ),
            )

        conn.execute(
            """INSERT INTO trade_log
               (market_id, asset_id, action, outcome, question, shares, price,
                usdc_amount, paper, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.market_id,
                trade.asset_id,
                "BUY",
                trade.outcome,
                trade.question,
                shares,
                trade.price,
                scaled_usdc,
                int(paper),
                int(time.time()),
            ),
        )
        if paper:
            self._adjust_paper_balance(-scaled_usdc)
        conn.commit()
        logger.info(
            "%sBUY recorded: %.2f shares @ %.3f ($%.2f) | %s",
            "PAPER " if paper else "",
            shares,
            trade.price,
            scaled_usdc,
            trade.question[:50],
        )

    def record_sell(
        self,
        trade: Trade,
        shares: float,
        proceeds_usdc: float,
        paper: bool = False,
    ) -> None:
        conn = db.get()
        existing = self.get(trade.market_id)
        realized_pnl = 0.0

        if existing and existing.shares > 0:
            realized_pnl = (trade.price - existing.avg_price) * shares
            new_shares = max(existing.shares - shares, 0)
            new_cost = new_shares * existing.avg_price

            if new_shares <= 0.0001:
                conn.execute(
                    "DELETE FROM positions WHERE market_id=?", (trade.market_id,)
                )
            else:
                conn.execute(
                    """UPDATE positions
                       SET shares=?, total_cost_usdc=?, updated_at=?
                       WHERE market_id=?""",
                    (new_shares, new_cost, int(time.time()), trade.market_id),
                )

        conn.execute(
            """INSERT INTO trade_log
               (market_id, asset_id, action, outcome, question, shares, price,
                usdc_amount, realized_pnl, paper, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.market_id,
                trade.asset_id,
                "SELL",
                trade.outcome,
                trade.question,
                shares,
                trade.price,
                proceeds_usdc,
                realized_pnl,
                int(paper),
                int(time.time()),
            ),
        )

        today = date.today().isoformat()
        conn.execute(
            """INSERT INTO daily_stats (date, realized_pnl_usdc)
               VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 realized_pnl_usdc = realized_pnl_usdc + excluded.realized_pnl_usdc""",
            (today, realized_pnl),
        )
        if paper:
            self._adjust_paper_balance(proceeds_usdc)
        conn.commit()
        logger.info(
            "%sSELL recorded: %.2f shares @ %.3f | P&L $%.2f | %s",
            "PAPER " if paper else "",
            shares,
            trade.price,
            realized_pnl,
            trade.question[:50],
        )

    def get(self, market_id: str) -> Optional[Position]:
        row = (
            db.get()
            .execute("SELECT * FROM positions WHERE market_id=?", (market_id,))
            .fetchone()
        )
        return _row_to_position(row) if row else None

    def all_open(self) -> list[Position]:
        rows = (
            db.get()
            .execute("SELECT * FROM positions WHERE shares > 0 ORDER BY opened_at DESC")
            .fetchall()
        )
        return [_row_to_position(r) for r in rows]

    def total_exposure_usdc(self) -> float:
        row = (
            db.get()
            .execute(
                "SELECT COALESCE(SUM(total_cost_usdc), 0) FROM positions WHERE shares > 0"
            )
            .fetchone()
        )
        return float(row[0])

    def today_pnl_usdc(self) -> float:
        today = date.today().isoformat()
        row = (
            db.get()
            .execute(
                "SELECT COALESCE(realized_pnl_usdc, 0) FROM daily_stats WHERE date=?",
                (today,),
            )
            .fetchone()
        )
        return float(row[0]) if row else 0.0

    def print_summary(self) -> None:
        positions = self.all_open()
        exposure = self.total_exposure_usdc()
        pnl = self.today_pnl_usdc()
        if config.PAPER_TRADE:
            balance = self.paper_balance()
            logger.info(
                "Paper balance: $%.2f | %d positions | exposure $%.2f | today P&L $%.2f",
                balance,
                len(positions),
                exposure,
                pnl,
            )
        else:
            logger.info(
                "Portfolio: %d open positions | exposure $%.2f | today P&L $%.2f",
                len(positions),
                exposure,
                pnl,
            )
        for p in positions:
            logger.info("  %s", p)


def _row_to_position(row) -> Position:
    return Position(
        market_id=row["market_id"],
        asset_id=row["asset_id"],
        question=row["question"] or "",
        outcome=row["outcome"] or "",
        shares=row["shares"],
        avg_price=row["avg_price"],
        total_cost_usdc=row["total_cost_usdc"],
        opened_at=row["opened_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------


class RiskManager:
    def __init__(self, tracker: PositionTracker) -> None:
        self.tracker = tracker

    def check(self, trade: Trade, scaled_usdc: float) -> tuple[bool, str]:
        """Return (approved, reason). Sells bypass most checks — they reduce risk."""
        if trade.action == "SELL":
            return self._check_daily_loss()

        # BUY checks
        ok, reason = self._check_daily_loss()
        if not ok:
            return False, reason

        ok, reason = self._check_position_size(trade, scaled_usdc)
        if not ok:
            return False, reason

        ok, reason = self._check_total_exposure(scaled_usdc)
        if not ok:
            return False, reason

        ok, reason = self._check_paper_balance(scaled_usdc)
        if not ok:
            return False, reason

        return True, ""

    def _check_daily_loss(self) -> tuple[bool, str]:
        pnl = self.tracker.today_pnl_usdc()
        if pnl < -config.DAILY_LOSS_LIMIT_USDC:
            return (
                False,
                f"Daily loss limit hit (${pnl:.2f} < -${config.DAILY_LOSS_LIMIT_USDC})",
            )
        return True, ""

    def _check_position_size(
        self, trade: Trade, scaled_usdc: float
    ) -> tuple[bool, str]:
        position = self.tracker.get(trade.market_id)
        current = position.total_cost_usdc if position else 0.0
        if current + scaled_usdc > config.MAX_POSITION_SIZE_USDC:
            return (
                False,
                f"Position size ${current + scaled_usdc:.2f} would exceed "
                f"max ${config.MAX_POSITION_SIZE_USDC}",
            )
        return True, ""

    def _check_total_exposure(self, scaled_usdc: float) -> tuple[bool, str]:
        total = self.tracker.total_exposure_usdc()
        if total + scaled_usdc > config.MAX_TOTAL_EXPOSURE_USDC:
            return (
                False,
                f"Total exposure ${total + scaled_usdc:.2f} would exceed "
                f"max ${config.MAX_TOTAL_EXPOSURE_USDC}",
            )
        return True, ""

    def _check_paper_balance(self, scaled_usdc: float) -> tuple[bool, str]:
        if not config.PAPER_TRADE:
            return True, ""
        balance = self.tracker.paper_balance()
        if scaled_usdc > balance:
            return False, f"Insufficient paper balance (${balance:.2f} available)"
        return True, ""
