"""
positions.py

PositionTracker — the repository for positions, trade_log, daily_stats and
paper_account. Every query filters by algo, so strategies stay disjoint.
"""

import logging
import time
from datetime import date
from typing import Optional

from . import db
from .domain.position import Position
from .domain.records import Trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position tracker
# ---------------------------------------------------------------------------


class PositionTracker:
    """Per-algorithm view of positions, trade log, and paper bankroll.

    Always pass an `algo` name in the constructor. Every read/write query
    filters by that algo so two trackers (one per algorithm) operate on
    disjoint slices of the same DB.
    """

    def __init__(self, algo: str = "copy_trade") -> None:
        self.algo = algo

    # ── Paper balance ────────────────────────────────────────────────────────

    def init_paper_balance(self, starting: float) -> None:
        """Seed balance only on first run; subsequent calls are no-ops."""
        conn = db.get()
        conn.execute(
            "INSERT OR IGNORE INTO paper_account (algo, balance, updated_at) "
            "VALUES (?, ?, ?)",
            (self.algo, starting, int(time.time())),
        )
        conn.commit()

    def paper_balance(self) -> float:
        row = db.get().execute(
            "SELECT balance FROM paper_account WHERE algo=?", (self.algo,),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def _adjust_paper_balance(self, delta: float) -> None:
        """Atomically adjust + commit the paper balance.

        Standalone callers (tests, scripts) get auto-commit behavior. Inside
        record_buy/record_sell we do NOT use this — we inline the balance
        UPDATE into the same transaction as the position/trade_log writes,
        so the whole record is atomic. Splitting into two commits would
        risk a half-recorded trade on crash.
        """
        conn = db.get()
        conn.execute(
            "UPDATE paper_account SET balance = balance + ?, updated_at = ? "
            "WHERE algo=?",
            (delta, int(time.time()), self.algo),
        )
        conn.commit()

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

        Caller (runner) provides the *actual* spent_usdc, shares, and
        fill_price from the exchange. We no longer derive shares from
        `trade.price` — that's the target's fill, not ours.
        """
        if shares <= 0 or fill_price <= 0:
            # Refuse to write a phantom position. The order may have been
            # sent already; surfacing loudly beats garbage in cost-basis math.
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
                   WHERE market_id=? AND paper=? AND algo=?""",
                (
                    new_shares, new_avg, new_cost, now,
                    trade.asset_id, trade.outcome,
                    trade.market_id, int(paper), self.algo,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO positions
                   (market_id, paper, algo, asset_id, question, outcome,
                    shares, avg_price, total_cost_usdc, opened_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.market_id, int(paper), self.algo, trade.asset_id,
                    trade.question, trade.outcome, shares, fill_price,
                    spent_usdc, now, now,
                ),
            )

        conn.execute(
            """INSERT INTO trade_log
               (market_id, asset_id, action, outcome, question, shares, price,
                usdc_amount, fee_usdc, paper, algo, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.market_id, trade.asset_id, "BUY", trade.outcome, trade.question,
                shares, fill_price, spent_usdc, fee_usdc,
                int(paper), self.algo, now,
            ),
        )
        if paper:
            # Inline the balance update so position + trade_log + balance
            # all land in ONE transaction. Splitting would leave a half-
            # recorded trade if the process dies mid-flight.
            conn.execute(
                "UPDATE paper_account SET balance = balance - ?, updated_at = ? "
                "WHERE algo=?",
                (spent_usdc + fee_usdc, now, self.algo),
            )
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
        """Record a completed SELL or REDEEM.

        Caller provides the *actual* shares closed and proceeds received.
        Realized P&L is computed against the stored avg_price.
        """
        conn = db.get()
        existing = self.get(trade.market_id, paper)
        realized_pnl = 0.0
        now = int(time.time())

        if existing and existing.shares > 0:
            cost_basis_sold = existing.avg_price * shares
            # Net proceeds (proceeds minus fee) minus cost basis = realized P&L.
            realized_pnl = (proceeds_usdc - fee_usdc) - cost_basis_sold

            new_shares = max(existing.shares - shares, 0.0)
            # Total cost scales linearly; avg_price is preserved.
            new_cost = new_shares * existing.avg_price

            if new_shares <= 0.0001:
                conn.execute(
                    "DELETE FROM positions WHERE market_id=? AND paper=? AND algo=?",
                    (trade.market_id, int(paper), self.algo),
                )
            else:
                conn.execute(
                    """UPDATE positions
                       SET shares=?, total_cost_usdc=?, updated_at=?
                       WHERE market_id=? AND paper=? AND algo=?""",
                    (
                        new_shares, new_cost, now,
                        trade.market_id, int(paper), self.algo,
                    ),
                )

        conn.execute(
            """INSERT INTO trade_log
               (market_id, asset_id, action, outcome, question, shares, price,
                usdc_amount, fee_usdc, realized_pnl, paper, algo, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.market_id, trade.asset_id, trade.action, trade.outcome,
                trade.question, shares, fill_price, proceeds_usdc, fee_usdc,
                realized_pnl, int(paper), self.algo, now,
            ),
        )

        today = date.today().isoformat()
        conn.execute(
            """INSERT INTO daily_stats (date, algo, realized_pnl_usdc)
               VALUES (?, ?, ?)
               ON CONFLICT(date, algo) DO UPDATE SET
                 realized_pnl_usdc = realized_pnl_usdc + excluded.realized_pnl_usdc""",
            (today, self.algo, realized_pnl),
        )
        if paper:
            # Inline for atomicity — see record_buy for the rationale.
            conn.execute(
                "UPDATE paper_account SET balance = balance + ?, updated_at = ? "
                "WHERE algo=?",
                (proceeds_usdc - fee_usdc, now, self.algo),
            )
        conn.commit()
        logger.info(
            "%s%s recorded: %.2f shares @ %.3f | P&L $%.2f | %s",
            "PAPER " if paper else "",
            trade.action,
            shares, fill_price, realized_pnl, trade.question[:50],
        )

    def get(self, market_id: str, paper: bool = False) -> Optional["Position"]:
        row = db.get().execute(
            "SELECT * FROM positions WHERE market_id=? AND paper=? AND algo=?",
            (market_id, int(paper), self.algo),
        ).fetchone()
        return _row_to_position(row) if row else None

    def all_open(self, paper: Optional[bool] = None) -> list["Position"]:
        conn = db.get()
        if paper is None:
            rows = conn.execute(
                "SELECT * FROM positions WHERE shares > 0 AND algo=? "
                "ORDER BY opened_at DESC",
                (self.algo,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions WHERE shares > 0 AND paper=? AND algo=? "
                "ORDER BY opened_at DESC",
                (int(paper), self.algo),
            ).fetchall()
        return [_row_to_position(r) for r in rows]

    def total_exposure_usdc(self, paper: Optional[bool] = None) -> float:
        conn = db.get()
        if paper is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_cost_usdc), 0) FROM positions "
                "WHERE shares > 0 AND algo=?",
                (self.algo,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_cost_usdc), 0) FROM positions "
                "WHERE shares > 0 AND paper=? AND algo=?",
                (int(paper), self.algo),
            ).fetchone()
        return float(row[0])

    def today_pnl_usdc(self, paper: Optional[bool] = None) -> float:
        """Realized P&L for today. Filter by paper-flag if provided."""
        today = date.today().isoformat()
        conn = db.get()
        if paper is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log "
                "WHERE algo=? AND date(ts,'unixepoch','localtime') = ?",
                (self.algo, today),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log "
                "WHERE paper=? AND algo=? AND date(ts,'unixepoch','localtime') = ?",
                (int(paper), self.algo, today),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def print_summary(self, paper: Optional[bool] = None) -> None:
        positions = self.all_open(paper=paper)
        exposure = self.total_exposure_usdc(paper=paper)
        pnl = self.today_pnl_usdc(paper=paper)
        tag = f"[{self.algo}] "
        if paper:
            balance = self.paper_balance()
            logger.info(
                "%sPaper balance: $%.2f | %d positions | exposure $%.2f | today P&L $%.2f",
                tag, balance, len(positions), exposure, pnl,
            )
        else:
            logger.info(
                "%sPortfolio: %d open positions | exposure $%.2f | today P&L $%.2f",
                tag, len(positions), exposure, pnl,
            )
        for p in positions:
            logger.info("%s  %s", tag, p)


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
