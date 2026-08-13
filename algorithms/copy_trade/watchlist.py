"""SQLite-backed scored-wallet watchlist for the copy-trade algorithm."""

import time
from typing import Iterable

from bot.storage import db

from .ranker import ResolvedBet, WalletScore, rank_wallets


_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_score_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    algo TEXT NOT NULL,
    persistence_passed INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wallet_scores (
    run_id INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    mean_edge REAL NOT NULL,
    standard_error REAL NOT NULL,
    edge_lower_bound REAL NOT NULL,
    copyability_score REAL NOT NULL,
    last_resolved_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, wallet)
);
CREATE TABLE IF NOT EXISTS copy_watchlist (
    algo TEXT NOT NULL,
    wallet TEXT NOT NULL,
    status TEXT NOT NULL,
    score_run_id INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (algo, wallet)
);
CREATE INDEX IF NOT EXISTS idx_copy_watchlist_active
    ON copy_watchlist(algo, status);
"""


class WatchlistRepository:
    def _init(self) -> None:
        db.get().executescript(_SCHEMA)
        db.get().commit()

    def active_wallets(self, algo: str) -> list[str]:
        self._init()
        rows = db.get().execute(
            "SELECT wallet FROM copy_watchlist WHERE algo=? AND status='active' ORDER BY wallet",
            (algo,),
        ).fetchall()
        return [str(row["wallet"]) for row in rows]

    def refresh(
        self, algo: str, bets: Iterable[ResolvedBet], *, watchlist_size: int,
        min_resolved_bets: int, confidence_z: float, min_copyability_score: float,
        persistence_passed: bool,
    ) -> list[WalletScore]:
        """Store a scored batch and atomically replace the active cohort."""
        self._init()
        scores = rank_wallets(bets, min_resolved_bets, confidence_z, min_copyability_score)
        now = int(time.time())
        conn = db.get()
        cur = conn.execute(
            "INSERT INTO wallet_score_runs (algo, persistence_passed, created_at) VALUES (?, ?, ?)",
            (algo, int(persistence_passed), now),
        )
        run_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO wallet_scores
               (run_id, wallet, sample_size, mean_edge, standard_error,
                edge_lower_bound, copyability_score, last_resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(run_id, s.wallet, s.sample_size, s.mean_edge, s.standard_error,
              s.edge_lower_bound, s.copyability_score, s.last_resolved_at) for s in scores],
        )
        conn.execute("UPDATE copy_watchlist SET status='demoted', updated_at=? WHERE algo=? AND status='active'", (now, algo))
        if persistence_passed:
            # Rank order alone would seat the least-bad wallet in a weak pool.
            # A non-positive lower bound means we cannot distinguish the wallet
            # from a coin flip, which is the entire point of computing one.
            selected = [s for s in scores if s.edge_lower_bound > 0][:watchlist_size]
            conn.executemany(
                """INSERT INTO copy_watchlist (algo, wallet, status, score_run_id, updated_at)
                   VALUES (?, ?, 'active', ?, ?)
                   ON CONFLICT(algo, wallet) DO UPDATE SET status='active', score_run_id=excluded.score_run_id, updated_at=excluded.updated_at""",
                [(algo, score.wallet, run_id, now) for score in selected],
            )
        conn.commit()
        return scores
