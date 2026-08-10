"""
lots.py

Partial-close accounting by attribution.

A position stays aggregated for exchange execution and P&L — one row per
market. Lots decompose it: each fill records the `source` that caused it,
so closing that source unwinds only its share instead of flattening the
whole position. Strategy-agnostic; `source` is an opaque key. Copy trading
passes a leader's wallet, but nothing here knows or cares.
"""

import time

from ..storage import db


def record_open(
    algo: str, market_id: str, asset_id: str, source: str,
    source_event_id: str, shares: float, cost_usdc: float,
) -> None:
    if not source or shares <= 0:
        return
    db.get().execute(
        """INSERT OR REPLACE INTO position_lots
           (algo, market_id, asset_id, source, source_event_id,
            shares, cost_usdc, remaining_shares, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (algo, market_id, asset_id, source.lower(), source_event_id,
         shares, cost_usdc, shares, int(time.time())),
    )
    db.get().commit()


def remaining_shares(algo: str, market_id: str, source: str) -> float:
    row = db.get().execute(
        """SELECT COALESCE(SUM(remaining_shares), 0) FROM position_lots
           WHERE algo=? AND market_id=? AND source=? AND remaining_shares > 0""",
        (algo, market_id, source.lower()),
    ).fetchone()
    return float(row[0]) if row else 0.0


def close_for_source(
    algo: str, market_id: str, source: str, shares: float,
) -> float:
    """Consume oldest open lots for one source; return shares attributed."""
    remaining = max(0.0, shares)
    consumed = 0.0
    conn = db.get()
    rows = conn.execute(
        """SELECT id, remaining_shares FROM position_lots
           WHERE algo=? AND market_id=? AND source=? AND remaining_shares > 0
           ORDER BY opened_at, id""",
        (algo, market_id, source.lower()),
    ).fetchall()
    for row in rows:
        if remaining <= 0:
            break
        used = min(remaining, float(row["remaining_shares"]))
        next_remaining = float(row["remaining_shares"]) - used
        conn.execute(
            "UPDATE position_lots SET remaining_shares=?, closed_at=CASE WHEN ? <= 0 THEN ? ELSE closed_at END WHERE id=?",
            (next_remaining, next_remaining, int(time.time()), row["id"]),
        )
        remaining -= used
        consumed += used
    conn.commit()
    return consumed


def close_market(algo: str, market_id: str) -> None:
    conn = db.get()
    conn.execute(
        """UPDATE position_lots SET remaining_shares=0, closed_at=?
           WHERE algo=? AND market_id=? AND remaining_shares > 0""",
        (int(time.time()), algo, market_id),
    )
    conn.commit()
