"""Leader-attributed lots for multi-wallet copy trading.

Positions stay aggregated for accounting and exchange execution.  Lots retain
which leader caused each copied fill so one leader's exit only reduces that
leader's allocation rather than flattening every leader in the market.
"""

import time

from . import db


def record_open(
    algo: str, market_id: str, asset_id: str, leader_wallet: str,
    leader_event_id: str, shares: float, cost_usdc: float,
) -> None:
    if not leader_wallet or shares <= 0:
        return
    db.get().execute(
        """INSERT OR REPLACE INTO copy_lots
           (algo, market_id, asset_id, leader_wallet, leader_event_id,
            shares, cost_usdc, remaining_shares, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (algo, market_id, asset_id, leader_wallet.lower(), leader_event_id,
         shares, cost_usdc, shares, int(time.time())),
    )
    db.get().commit()


def remaining_shares(algo: str, market_id: str, leader_wallet: str) -> float:
    row = db.get().execute(
        """SELECT COALESCE(SUM(remaining_shares), 0) FROM copy_lots
           WHERE algo=? AND market_id=? AND leader_wallet=? AND remaining_shares > 0""",
        (algo, market_id, leader_wallet.lower()),
    ).fetchone()
    return float(row[0]) if row else 0.0


def close_for_leader(
    algo: str, market_id: str, leader_wallet: str, shares: float,
) -> float:
    """Consume oldest open lots for a leader and return shares attributed."""
    remaining = max(0.0, shares)
    consumed = 0.0
    conn = db.get()
    rows = conn.execute(
        """SELECT id, remaining_shares FROM copy_lots
           WHERE algo=? AND market_id=? AND leader_wallet=? AND remaining_shares > 0
           ORDER BY opened_at, id""",
        (algo, market_id, leader_wallet.lower()),
    ).fetchall()
    for row in rows:
        if remaining <= 0:
            break
        used = min(remaining, float(row["remaining_shares"]))
        next_remaining = float(row["remaining_shares"]) - used
        conn.execute(
            "UPDATE copy_lots SET remaining_shares=?, closed_at=CASE WHEN ? <= 0 THEN ? ELSE closed_at END WHERE id=?",
            (next_remaining, next_remaining, int(time.time()), row["id"]),
        )
        remaining -= used
        consumed += used
    conn.commit()
    return consumed


def close_market(algo: str, market_id: str) -> None:
    conn = db.get()
    conn.execute(
        """UPDATE copy_lots SET remaining_shares=0, closed_at=?
           WHERE algo=? AND market_id=? AND remaining_shares > 0""",
        (int(time.time()), algo, market_id),
    )
    conn.commit()
