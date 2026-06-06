"""
SQLite connection + schema for the copy-trading bot.

Notes on the choices here:
  * WAL journal mode so the notifier's daily-summary thread can read while
    the main thread is writing — without WAL, concurrent reads/writes can
    raise `database is locked`.
  * synchronous=NORMAL gives a major write-throughput win with a tiny
    durability tradeoff that is irrelevant for bet-sized data we can
    reconstruct from the activity API anyway.
  * Indexes on trade_log because the summary script and risk checks filter
    by (paper, ts) and (market_id); without them the table is a full scan.
"""

import sqlite3

from . import config

_conn: sqlite3.Connection | None = None


def get() -> sqlite3.Connection:
    """Return the shared SQLite connection, initializing on first call."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # Concurrency pragmas must be set per-connection.
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        # Foreign keys aren't used here but enabling them is cheap and future-proof.
        _conn.execute("PRAGMA foreign_keys=ON;")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            market_id        TEXT,
            paper            INTEGER NOT NULL DEFAULT 0,
            asset_id         TEXT NOT NULL,
            question         TEXT,
            outcome          TEXT,
            shares           REAL NOT NULL DEFAULT 0,
            avg_price        REAL NOT NULL DEFAULT 0,
            total_cost_usdc  REAL NOT NULL DEFAULT 0,
            opened_at        INTEGER,
            updated_at       INTEGER,
            PRIMARY KEY (market_id, paper)
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
            ts           INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date              TEXT PRIMARY KEY,
            realized_pnl_usdc REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS paper_account (
            id      INTEGER PRIMARY KEY CHECK (id = 1),
            balance REAL NOT NULL
        );

        -- Hot paths: filter by (paper, ts) for "today" P&L; filter by market_id
        -- when rebuilding a position. Both benefit hugely from these indexes.
        CREATE INDEX IF NOT EXISTS idx_trade_log_paper_ts
            ON trade_log(paper, ts);
        CREATE INDEX IF NOT EXISTS idx_trade_log_market
            ON trade_log(market_id);
        CREATE INDEX IF NOT EXISTS idx_positions_paper_open
            ON positions(paper, shares);
    """)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place column additions for upgrading from older schemas.

    Safe to run on every startup — each check is idempotent.
    """
    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    for col in ("outcome", "question"):
        if col not in trade_cols:
            conn.execute(f"ALTER TABLE trade_log ADD COLUMN {col} TEXT")
    if "fee_usdc" not in trade_cols:
        # Older rows have no fee data; default to 0 so historical P&L is unchanged.
        conn.execute("ALTER TABLE trade_log ADD COLUMN fee_usdc REAL DEFAULT 0")

    pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "paper" not in pos_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN paper INTEGER NOT NULL DEFAULT 0")

    conn.commit()
