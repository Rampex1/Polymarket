"""
Position tracking + risk management.

Two important design rules enforced here:

  1. The `paper` flag is *passed in* by the caller (it is resolved once in
     main.py at startup). This module never reads `config.PAPER_TRADE`
     directly — that was a source of subtle bugs where some checks read
     global config while others used the per-call argument.

  2. Sells/redeems never block on risk checks. A sell *reduces* exposure
     and any associated risk; blocking it (e.g. because we're down on the
     day) would lock us into a losing position. Only BUYs are gated.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import config, db
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
    def cost_basis_usdc(self) -> float:
        """USDC originally spent acquiring these shares (not mark-to-market).

        Renamed from the old `current_value_usdc` which was misleading —
        mark-to-market value requires a live mid-price lookup; this property
        is just the recorded cost basis (`shares * avg_price`).
        """
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
        conn = db.get()
        conn.execute(
            "INSERT OR IGNORE INTO paper_account (id, balance) VALUES (1, ?)",
            (starting,),
        )
        conn.commit()

    def paper_balance(self) -> float:
        row = db.get().execute(
            "SELECT balance FROM paper_account WHERE id=1"
        ).fetchone()
        return float(row[0]) if row else 0.0

    def _adjust_paper_balance(self, delta: float) -> None:
        # NOTE: caller is responsible for committing — this method is always
        # invoked inside a larger transaction (e.g. record_buy).
        db.get().execute(
            "UPDATE paper_account SET balance = balance + ? WHERE id=1", (delta,)
        )

    # ── Trade recording ──────────────────────────────────────────────────────

    def record_buy(
        self,
        trade: Trade,
        spent_usdc: float,
        shares: float,
        fill_price: float,
        paper: bool = False,
        fee_usdc: float = 0.0,
    ) -> None:
        """Record a completed BUY.

        The caller (executor) provides the *actual* spent_usdc, shares, and
        fill_price returned by the exchange — we no longer derive shares
        from `trade.price` (which is the target's fill, not ours).
        """
        if shares <= 0 or fill_price <= 0:
            # Refuse to record a phantom position. The caller has already
            # placed the order; better to surface this loudly than to write
            # garbage into the cost-basis math.
            logger.error(
                "record_buy refused: shares=%.4f price=%.4f (trade %s)",
                shares, fill_price, trade.id,
            )
            return

        conn = db.get()
        existing = self.get(trade.market_id, paper)
        now = int(time.time())

        if existing:
            new_shares = existing.shares + shares
            new_cost = existing.total_cost_usdc + spent_usdc
            new_avg = new_cost / new_shares if new_shares > 0 else 0
            conn.execute(
                """UPDATE positions
                   SET shares=?, avg_price=?, total_cost_usdc=?, updated_at=?,
                       asset_id=?, outcome=?
                   WHERE market_id=? AND paper=?""",
                (
                    new_shares, new_avg, new_cost, now,
                    trade.asset_id, trade.outcome,
                    trade.market_id, int(paper),
                ),
            )
        else:
            conn.execute(
                """INSERT INTO positions
                   (market_id, paper, asset_id, question, outcome, shares, avg_price,
                    total_cost_usdc, opened_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.market_id, int(paper), trade.asset_id, trade.question,
                    trade.outcome, shares, fill_price, spent_usdc, now, now,
                ),
            )

        conn.execute(
            """INSERT INTO trade_log
               (market_id, asset_id, action, outcome, question, shares, price,
                usdc_amount, fee_usdc, paper, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.market_id, trade.asset_id, "BUY", trade.outcome, trade.question,
                shares, fill_price, spent_usdc, fee_usdc, int(paper), now,
            ),
        )
        if paper:
            # Subtract spend *plus* fee from the virtual balance so paper
            # accounting reflects what live would actually cost.
            self._adjust_paper_balance(-(spent_usdc + fee_usdc))
        conn.commit()
        logger.info(
            "%sBUY recorded: %.2f shares @ %.3f ($%.2f, fee $%.2f) | %s",
            "PAPER " if paper else "",
            shares, fill_price, spent_usdc, fee_usdc, trade.question[:50],
        )

    def record_sell(
        self,
        trade: Trade,
        shares: float,
        proceeds_usdc: float,
        fill_price: float,
        paper: bool = False,
        fee_usdc: float = 0.0,
    ) -> None:
        """Record a completed SELL.

        Caller provides the *actual* shares sold and proceeds. We compute
        realized P&L against the stored avg_price.
        """
        conn = db.get()
        existing = self.get(trade.market_id, paper)
        realized_pnl = 0.0
        now = int(time.time())

        if existing and existing.shares > 0:
            # P&L is on the proceeds net of fees minus cost basis for the
            # shares being sold.
            cost_basis_sold = existing.avg_price * shares
            realized_pnl = (proceeds_usdc - fee_usdc) - cost_basis_sold

            new_shares = max(existing.shares - shares, 0.0)
            # Cost basis for remaining shares scales with shares — avg_price is preserved.
            new_cost = new_shares * existing.avg_price

            if new_shares <= 0.0001:
                conn.execute(
                    "DELETE FROM positions WHERE market_id=? AND paper=?",
                    (trade.market_id, int(paper)),
                )
            else:
                conn.execute(
                    """UPDATE positions
                       SET shares=?, total_cost_usdc=?, updated_at=?
                       WHERE market_id=? AND paper=?""",
                    (new_shares, new_cost, now, trade.market_id, int(paper)),
                )

        conn.execute(
            """INSERT INTO trade_log
               (market_id, asset_id, action, outcome, question, shares, price,
                usdc_amount, fee_usdc, realized_pnl, paper, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.market_id, trade.asset_id, trade.action, trade.outcome,
                trade.question, shares, fill_price, proceeds_usdc, fee_usdc,
                realized_pnl, int(paper), now,
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
            # Proceeds are already net-of-fee on the exchange side; we model
            # the fee separately so paper balances stay self-consistent.
            self._adjust_paper_balance(proceeds_usdc - fee_usdc)
        conn.commit()
        logger.info(
            "%s%s recorded: %.2f shares @ %.3f | P&L $%.2f | %s",
            "PAPER " if paper else "",
            trade.action,
            shares, fill_price, realized_pnl, trade.question[:50],
        )

    def get(self, market_id: str, paper: bool = False) -> Optional["Position"]:
        row = db.get().execute(
            "SELECT * FROM positions WHERE market_id=? AND paper=?",
            (market_id, int(paper)),
        ).fetchone()
        return _row_to_position(row) if row else None

    def all_open(self, paper: Optional[bool] = None) -> list["Position"]:
        conn = db.get()
        if paper is None:
            rows = conn.execute(
                "SELECT * FROM positions WHERE shares > 0 ORDER BY opened_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions WHERE shares > 0 AND paper=? ORDER BY opened_at DESC",
                (int(paper),),
            ).fetchall()
        return [_row_to_position(r) for r in rows]

    def total_exposure_usdc(self, paper: Optional[bool] = None) -> float:
        conn = db.get()
        if paper is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_cost_usdc), 0) FROM positions WHERE shares > 0"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_cost_usdc), 0) FROM positions "
                "WHERE shares > 0 AND paper=?",
                (int(paper),),
            ).fetchone()
        return float(row[0])

    def today_pnl_usdc(self, paper: Optional[bool] = None) -> float:
        """Realized P&L for today. Filter by paper-flag if provided.

        Pulled from `trade_log` (not `daily_stats`) so paper and live can be
        separated cleanly — `daily_stats` is intentionally a combined number
        for legacy compatibility.
        """
        today = date.today().isoformat()
        conn = db.get()
        if paper is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log "
                "WHERE date(ts,'unixepoch','localtime') = ?",
                (today,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log "
                "WHERE paper=? AND date(ts,'unixepoch','localtime') = ?",
                (int(paper), today),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def print_summary(self, paper: Optional[bool] = None) -> None:
        positions = self.all_open(paper=paper)
        exposure = self.total_exposure_usdc(paper=paper)
        pnl = self.today_pnl_usdc(paper=paper)
        if paper:
            balance = self.paper_balance()
            logger.info(
                "Paper balance: $%.2f | %d positions | exposure $%.2f | today P&L $%.2f",
                balance, len(positions), exposure, pnl,
            )
        else:
            logger.info(
                "Portfolio: %d open positions | exposure $%.2f | today P&L $%.2f",
                len(positions), exposure, pnl,
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

    def check(
        self,
        trade: Trade,
        scaled_usdc: float,
        paper: bool = False,
    ) -> tuple[bool, str]:
        """Return (approved, reason).

        Sells and redeems unconditionally pass — they reduce exposure and the
        daily loss limit should not be able to lock the bot into a losing
        position.
        """
        if trade.action != "BUY":
            return True, ""

        # ── BUY checks (in escalation order) ────────────────────────────────
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
        """Enforce Polymarket's protocol-level order minimum.

        Without this, sub-$1 tier bets get sent to CLOB and bounce — wasting
        an API round trip and producing a spurious "BUY order failed" log.
        """
        if scaled_usdc < config.MIN_ORDER_SIZE_USDC:
            return (
                False,
                f"Scaled size ${scaled_usdc:.2f} below min "
                f"${config.MIN_ORDER_SIZE_USDC:.2f}",
            )
        return True, ""

    def _check_daily_loss(self, paper: bool) -> tuple[bool, str]:
        pnl = self.tracker.today_pnl_usdc(paper=paper)
        if pnl < -config.DAILY_LOSS_LIMIT_USDC:
            return (
                False,
                f"Daily loss limit hit (${pnl:.2f} < -${config.DAILY_LOSS_LIMIT_USDC})",
            )
        return True, ""

    def _check_position_size(
        self, trade: Trade, scaled_usdc: float, paper: bool
    ) -> tuple[bool, str]:
        position = self.tracker.get(trade.market_id, paper)
        current = position.total_cost_usdc if position else 0.0
        if current + scaled_usdc > config.MAX_POSITION_SIZE_USDC:
            return (
                False,
                f"Position size ${current + scaled_usdc:.2f} would exceed "
                f"max ${config.MAX_POSITION_SIZE_USDC}",
            )
        return True, ""

    def _check_total_exposure(
        self, scaled_usdc: float, paper: bool
    ) -> tuple[bool, str]:
        total = self.tracker.total_exposure_usdc(paper)
        if total + scaled_usdc > config.MAX_TOTAL_EXPOSURE_USDC:
            return (
                False,
                f"Total exposure ${total + scaled_usdc:.2f} would exceed "
                f"max ${config.MAX_TOTAL_EXPOSURE_USDC}",
            )
        return True, ""

    def _check_paper_balance(
        self, scaled_usdc: float, paper: bool
    ) -> tuple[bool, str]:
        # Live mode has its own on-chain balance check via the exchange; only
        # paper needs us to gate manually.
        if not paper:
            return True, ""
        balance = self.tracker.paper_balance()
        if scaled_usdc > balance:
            return False, f"Insufficient paper balance (${balance:.2f} available)"
        return True, ""
