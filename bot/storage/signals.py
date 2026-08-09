"""
Signal feature logging — the training-data store.

Every OpenIntent the runner dispatches leaves one row in `signals`: the raw
observables the algorithm captured at signal time (`intent.features`), whether
the order executed, and — backfilled at settlement — the market's close price
and our realized P&L. Months of these rows are what the future confidence
model trains on; they cannot be reconstructed after the fact (balances change,
books move, the firehose window slides).

Two hard rules:
  * Log RAW observables, not derived scores. Derivations (Kelly-implied
    belief, edge) can be recomputed offline forever; raw inputs can't be
    re-observed.
  * Recording must NEVER break dispatch. Every public function swallows and
    logs its own exceptions; a feature-logging bug must not cost a trade.
"""

import json
import logging
import time
from typing import Optional

from . import db

logger = logging.getLogger(__name__)


def record(
    algo_name: str,
    intent,
    paper: bool,
    executed: bool,
    skip_reason: Optional[str] = None,
) -> None:
    """Upsert one signal row keyed by (signal_id, algo).

    Re-dispatch of the same signal updates the execution outcome rather than
    duplicating. Intents without a signal_id are dropped — no dedupe key.
    """
    try:
        signal_id = getattr(intent, "signal_id", "") or ""
        if not signal_id:
            return

        # default=str coerces anything JSON can't represent — a weird feature
        # value must degrade to a string, never to a lost row.
        features_json = json.dumps(getattr(intent, "features", {}) or {}, default=str)

        db.get().execute(
            """
            INSERT INTO signals
                (signal_id, algo, paper, ts, market_id, asset_id, question,
                 signal_price, usdc_amount, features, executed, skip_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_id, algo) DO UPDATE SET
                executed = excluded.executed,
                skip_reason = excluded.skip_reason,
                features = excluded.features
            """,
            (
                signal_id,
                algo_name,
                1 if paper else 0,
                int(time.time()),
                getattr(intent, "market_id", ""),
                getattr(intent, "asset_id", "") or "",
                getattr(intent, "question", ""),
                float(getattr(intent, "signal_price", 0.0) or 0.0),
                float(getattr(intent, "usdc_amount", 0.0) or 0.0),
                features_json,
                1 if executed else 0,
                skip_reason,
            ),
        )
        db.get().commit()
    except Exception:
        logger.exception("[%s] Failed to record signal — continuing", algo_name)


def label_outcomes(
    algo_name: str,
    market_id: str,
    close_price: float,
    pnl_usdc: float,
    paper: bool,
) -> int:
    """Backfill outcome + P&L on this market's unlabeled signal rows.

    Idempotent — only rows with `outcome IS NULL` are touched, so a re-settle
    or replay can't overwrite the original label. Returns rows labeled.
    """
    try:
        cur = db.get().execute(
            """
            UPDATE signals
            SET outcome = ?, outcome_ts = ?, pnl_usdc = ?
            WHERE algo = ? AND market_id = ? AND paper = ? AND outcome IS NULL
            """,
            (
                float(close_price),
                int(time.time()),
                float(pnl_usdc),
                algo_name,
                market_id,
                1 if paper else 0,
            ),
        )
        db.get().commit()
        return cur.rowcount
    except Exception:
        logger.exception("[%s] Failed to label signal outcomes — continuing", algo_name)
        return 0


def unlabeled_market_ids(algo_name: str, paper: bool) -> list[str]:
    """Markets with signal rows still awaiting an outcome label.

    Skipped signals (risk-blocked, slippage) never become positions, so the
    settle path never labels them — a future batch job resolves these against
    Gamma. This query is its work list.
    """
    try:
        rows = db.get().execute(
            """
            SELECT DISTINCT market_id FROM signals
            WHERE algo = ? AND paper = ? AND outcome IS NULL
            ORDER BY market_id
            """,
            (algo_name, 1 if paper else 0),
        ).fetchall()
        return [r["market_id"] for r in rows]
    except Exception:
        logger.exception("[%s] Failed to list unlabeled markets", algo_name)
        return []
