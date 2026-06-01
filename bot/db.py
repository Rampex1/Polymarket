import sqlite3
from . import config

_conn: sqlite3.Connection | None = None


def get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            market_id        TEXT PRIMARY KEY,
            asset_id         TEXT NOT NULL,
            question         TEXT,
            outcome          TEXT,
            shares           REAL NOT NULL DEFAULT 0,
            avg_price        REAL NOT NULL DEFAULT 0,
            total_cost_usdc  REAL NOT NULL DEFAULT 0,
            opened_at        INTEGER,
            updated_at       INTEGER
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
    """)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    for col in ("outcome", "question"):
        if col not in cols:
            conn.execute(f"ALTER TABLE trade_log ADD COLUMN {col} TEXT")
    conn.commit()
