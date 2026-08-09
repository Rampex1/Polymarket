"""
db.py

Thread-local SQLite connections and the schema, created on every connect.
"""

import os
import sqlite3
import threading

from . import config

_local = threading.local()


def get() -> sqlite3.Connection:
    """Return *this thread's* SQLite connection, opening one on first call."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        parent = os.path.dirname(config.DB_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Pragmas must be set per-connection.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _init_schema(conn)
        _local.conn = conn
    return conn


def reset_for_tests() -> None:
    """Close the current thread's connection and forget it.

    Tests use this between cases to point at a new tmp DB without leaking
    a connection to the previous file. Production code should never call it.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            market_id        TEXT,
            paper            INTEGER NOT NULL DEFAULT 0,
            algo             TEXT NOT NULL,
            asset_id         TEXT NOT NULL,
            question         TEXT,
            outcome          TEXT,
            shares           REAL NOT NULL DEFAULT 0,
            avg_price        REAL NOT NULL DEFAULT 0,
            total_cost_usdc  REAL NOT NULL DEFAULT 0,
            opened_at        INTEGER,
            updated_at       INTEGER,
            PRIMARY KEY (market_id, paper, algo)
        );

        CREATE TABLE IF NOT EXISTS trade_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id    TEXT,
            asset_id     TEXT,
            action       TEXT,
            outcome      TEXT,
            question     TEXT,
            shares       REAL,
            price        REAL,
            usdc_amount  REAL,
            fee_usdc     REAL DEFAULT 0,
            realized_pnl REAL DEFAULT 0,
            paper        INTEGER DEFAULT 0,
            algo         TEXT NOT NULL,
            ts           INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date              TEXT,
            algo              TEXT NOT NULL,
            realized_pnl_usdc REAL DEFAULT 0,
            PRIMARY KEY (date, algo)
        );

        CREATE TABLE IF NOT EXISTS paper_account (
            algo       TEXT PRIMARY KEY,
            balance    REAL NOT NULL,
            updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS signals (
            signal_id    TEXT NOT NULL,
            algo         TEXT NOT NULL,
            paper        INTEGER NOT NULL DEFAULT 1,
            ts           INTEGER NOT NULL,
            market_id    TEXT,
            asset_id     TEXT,
            question     TEXT,
            signal_price REAL,
            usdc_amount  REAL,
            features     TEXT NOT NULL DEFAULT '{}',
            executed     INTEGER NOT NULL DEFAULT 0,
            skip_reason  TEXT,
            outcome      REAL,
            outcome_ts   INTEGER,
            pnl_usdc     REAL,
            PRIMARY KEY (signal_id, algo)
        );
        CREATE INDEX IF NOT EXISTS idx_signals_market
            ON signals(algo, market_id);

        CREATE TABLE IF NOT EXISTS copy_lots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            algo             TEXT NOT NULL,
            market_id        TEXT NOT NULL,
            asset_id         TEXT NOT NULL,
            leader_wallet    TEXT NOT NULL,
            leader_event_id  TEXT NOT NULL,
            shares           REAL NOT NULL,
            cost_usdc        REAL NOT NULL,
            remaining_shares REAL NOT NULL,
            opened_at        INTEGER NOT NULL,
            closed_at        INTEGER,
            UNIQUE(algo, leader_event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_copy_lots_open
            ON copy_lots(algo, market_id, leader_wallet, remaining_shares);

        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            algo        TEXT NOT NULL,
            mode        TEXT NOT NULL,
            profile     TEXT,
            git_sha     TEXT,
            params_json TEXT NOT NULL,
            started_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_algo ON runs(algo, started_at);

        CREATE TABLE IF NOT EXISTS discord_threads (
            market_id  TEXT    NOT NULL,
            algo       TEXT    NOT NULL,
            paper      INTEGER NOT NULL,
            thread_id  TEXT    NOT NULL,
            created_at INTEGER,
            PRIMARY KEY (market_id, algo, paper)
        );

        CREATE INDEX IF NOT EXISTS idx_trade_log_paper_ts
            ON trade_log(paper, ts);
        CREATE INDEX IF NOT EXISTS idx_trade_log_market
            ON trade_log(market_id);
        CREATE INDEX IF NOT EXISTS idx_positions_paper_open
            ON positions(paper, shares);
    """)
    conn.commit()
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_trade_log_algo ON trade_log(algo);
        CREATE INDEX IF NOT EXISTS idx_positions_algo ON positions(algo);
    """)
    conn.commit()
