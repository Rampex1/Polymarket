"""
SQLite connection + schema for the copy-trading bot.

Connection strategy
-------------------
Each thread gets its **own** connection via `threading.local`. The previous
implementation shared a single connection across threads with
`check_same_thread=False`. WAL mode allows concurrent *connections* to read
while a writer holds the lock — but it does NOT make a *single shared
connection* safe across threads. Interleaved `execute()` calls on one
connection can still corrupt the cursor state. The thread-local pattern
fixes this properly.

Pragmas (per connection)
------------------------
  * WAL journal mode → concurrent readers + single writer.
  * synchronous=NORMAL → big write-throughput win, durability tradeoff
    irrelevant for bet-sized data we can reconstruct from the activity API.
  * Indexes on hot paths (paper+ts, market_id) — without them the summary
    script and risk checks become full-table scans.
"""

import sqlite3
import threading

from . import config

# Each thread sees its own connection via attribute lookup on this object.
_local = threading.local()


def get() -> sqlite3.Connection:
    """Return *this thread's* SQLite connection, opening one on first call."""
    conn = getattr(_local, "conn", None)
    if conn is None:
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
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            balance    REAL NOT NULL,
            updated_at INTEGER
        );

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

    Each check is idempotent so re-running is harmless.
    """
    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    for col in ("outcome", "question"):
        if col not in trade_cols:
            conn.execute(f"ALTER TABLE trade_log ADD COLUMN {col} TEXT")
    if "fee_usdc" not in trade_cols:
        conn.execute("ALTER TABLE trade_log ADD COLUMN fee_usdc REAL DEFAULT 0")

    pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "paper" not in pos_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN paper INTEGER NOT NULL DEFAULT 0")

    pa_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_account)")}
    if "updated_at" not in pa_cols:
        conn.execute("ALTER TABLE paper_account ADD COLUMN updated_at INTEGER")

    conn.commit()
