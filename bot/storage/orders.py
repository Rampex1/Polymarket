"""Resting maker orders — placed, not yet filled, and therefore not positions.

A taker order is a position the moment it comes back matched, so the runner
never needed to remember one. A maker order is different: it sits on the book
for minutes, may fill in part or not at all, and must be cancelled if it goes
stale. Until it fills it is *not* in `positions`, and nothing downstream —
exposure, risk, the settle sweep — should think it is.

The row stores everything needed to rebuild the originating `OpenIntent`, so
when a fill lands the runner replays the ordinary fill path (`signals.record`,
`ledger.record_buy`, `lots.record_open`, the Discord alert) instead of a
parallel one that could drift from it.
"""

import json
import logging
import time
from typing import Optional

from ..domain.intents import OpenIntent
from . import db

logger = logging.getLogger(__name__)


def record(
    order_id: str,
    algo_name: str,
    paper: bool,
    intent: OpenIntent,
    limit_price: float,
    size: float,
) -> None:
    """Remember an order that is resting on the book."""
    conn = db.get()
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO open_orders
                (order_id, algo, paper, market_id, asset_id, outcome, question,
                 limit_price, size, usdc_amount, signal_id, signal_price,
                 features, source, source_event_id, placed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_id, algo_name, 1 if paper else 0,
                intent.market_id, intent.asset_id or "", intent.outcome,
                intent.question, float(limit_price), float(size),
                float(intent.usdc_amount), intent.signal_id,
                float(intent.signal_price or 0.0),
                json.dumps(intent.features or {}, default=str),
                intent.source, intent.source_event_id, int(time.time()),
            ),
        )


class Resting:
    """One resting order, with the intent that produced it rebuilt."""

    __slots__ = ("order_id", "algo", "paper", "limit_price", "size",
                 "placed_at", "intent")

    def __init__(self, row):
        self.order_id = row["order_id"]
        self.algo = row["algo"]
        self.paper = bool(row["paper"])
        self.limit_price = float(row["limit_price"])
        self.size = float(row["size"])
        self.placed_at = int(row["placed_at"])
        try:
            features = json.loads(row["features"] or "{}")
        except (TypeError, ValueError):
            features = {}
        self.intent = OpenIntent(
            market_id=row["market_id"],
            asset_id=row["asset_id"],
            usdc_amount=float(row["usdc_amount"]),
            signal_price=float(row["signal_price"]),
            question=row["question"] or "",
            outcome=row["outcome"] or "",
            signal_id=row["signal_id"] or "",
            features=features,
            source=row["source"] or "",
            source_event_id=row["source_event_id"] or "",
        )

    def age_seconds(self) -> float:
        return time.time() - self.placed_at


def resting(algo_name: str, paper: bool) -> list[Resting]:
    rows = db.get().execute(
        "SELECT * FROM open_orders WHERE algo = ? AND paper = ? ORDER BY placed_at",
        (algo_name, 1 if paper else 0),
    ).fetchall()
    return [Resting(r) for r in rows]


def held_market_ids(algo_name: str, paper: bool) -> set[str]:
    """Markets with an order already working.

    A maker strategy polls far faster than its orders fill, so without this
    every poll would stack another order on the same market and the per-event
    cap would count none of them.
    """
    rows = db.get().execute(
        "SELECT DISTINCT market_id FROM open_orders WHERE algo = ? AND paper = ?",
        (algo_name, 1 if paper else 0),
    ).fetchall()
    return {r["market_id"] for r in rows}


def drop(order_id: str, algo_name: str) -> None:
    conn = db.get()
    with conn:
        conn.execute("DELETE FROM open_orders WHERE order_id = ? AND algo = ?",
                     (order_id, algo_name))


def count(algo_name: str, paper: bool) -> int:
    row = db.get().execute(
        "SELECT COUNT(*) AS n FROM open_orders WHERE algo = ? AND paper = ?",
        (algo_name, 1 if paper else 0),
    ).fetchone()
    return int(row["n"])
