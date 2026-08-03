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

import os
import sqlite3
import threading

from . import config

# Each thread sees its own connection via attribute lookup on this object.
_local = threading.local()


def get() -> sqlite3.Connection:
    """Return *this thread's* SQLite connection, opening one on first call."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        # sqlite3.connect fails on a missing directory — create it so the
        # data/ default works on a fresh checkout.
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
    # Note: `positions` and `paper_account` ship with the v2 schema (algo
    # column / per-algo keying) for fresh installs. The _migrate() pass
    # upgrades existing v1 databases to match.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            market_id        TEXT,
            paper            INTEGER NOT NULL DEFAULT 0,
            algo             TEXT NOT NULL DEFAULT 'copy_trade',
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
            algo         TEXT NOT NULL DEFAULT 'copy_trade',
            ts           INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date              TEXT,
            algo              TEXT NOT NULL DEFAULT 'copy_trade',
            realized_pnl_usdc REAL DEFAULT 0,
            PRIMARY KEY (date, algo)
        );

        CREATE TABLE IF NOT EXISTS paper_account (
            algo       TEXT PRIMARY KEY,
            balance    REAL NOT NULL,
            updated_at INTEGER
        );

        -- Training-data store: one row per signal the runner dispatched,
        -- raw features captured at signal time, outcome backfilled at
        -- settlement. See bot/signals.py.
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

        -- Leader attribution for the multi-wallet copy-trade strategy.
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

        -- One row per worker boot: the exact resolved params (JSON) plus
        -- git sha, so analytics can attribute every position/signal to the
        -- config version that produced it. See bot/runs.py.
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

        -- Discord thread registry: maps (market_id, algo, paper) → thread_id
        -- so position-lifecycle notifications route into the thread instead of
        -- flooding the main channel. See bot/threads.py.
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
    _migrate(conn)
    # Algo indexes are created AFTER the migration so legacy DBs (where
    # `algo` doesn't exist on the original tables yet) have the column
    # added before we try to index it.
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_trade_log_algo ON trade_log(algo);
        CREATE INDEX IF NOT EXISTS idx_positions_algo ON positions(algo);
    """)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place schema upgrades for older databases.

    Each step is idempotent so re-running is harmless.
    """
    # ── trade_log column adds (v0 → v1) ──────────────────────────────────────
    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    for col in ("outcome", "question"):
        if col not in trade_cols:
            conn.execute(f"ALTER TABLE trade_log ADD COLUMN {col} TEXT")
    if "fee_usdc" not in trade_cols:
        conn.execute("ALTER TABLE trade_log ADD COLUMN fee_usdc REAL DEFAULT 0")

    # ── positions paper column (v0 → v1) ─────────────────────────────────────
    pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "paper" not in pos_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN paper INTEGER NOT NULL DEFAULT 0")

    # ── v1 → v2: algo column on positions, trade_log, daily_stats ────────────
    # Backfill defaults to 'copy_trade' so existing single-algo bots keep
    # working without intervention.
    pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "algo" not in pos_cols:
        conn.execute(
            "ALTER TABLE positions ADD COLUMN algo TEXT NOT NULL DEFAULT 'copy_trade'"
        )

    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    if "algo" not in trade_cols:
        conn.execute(
            "ALTER TABLE trade_log ADD COLUMN algo TEXT NOT NULL DEFAULT 'copy_trade'"
        )

    ds_cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_stats)")}
    if "algo" not in ds_cols:
        # daily_stats had a single-column PK (date). Repartition to (date, algo)
        # via the table-rebuild dance — SQLite can't ALTER PRIMARY KEY in place.
        conn.executescript("""
            CREATE TABLE daily_stats_v2 (
                date              TEXT,
                algo              TEXT NOT NULL DEFAULT 'copy_trade',
                realized_pnl_usdc REAL DEFAULT 0,
                PRIMARY KEY (date, algo)
            );
            INSERT INTO daily_stats_v2 (date, algo, realized_pnl_usdc)
                SELECT date, 'copy_trade', realized_pnl_usdc FROM daily_stats;
            DROP TABLE daily_stats;
            ALTER TABLE daily_stats_v2 RENAME TO daily_stats;
        """)

    # ── paper_account: singleton id=1 → per-algo (PK=algo) ───────────────────
    pa_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_account)")}
    if "id" in pa_cols and "algo" not in pa_cols:
        # Old shape: (id PK, balance, updated_at). Migrate the single row
        # to a new table keyed by algo, preserving the existing balance
        # under the 'copy_trade' attribution.
        conn.executescript("""
            CREATE TABLE paper_account_v2 (
                algo       TEXT PRIMARY KEY,
                balance    REAL NOT NULL,
                updated_at INTEGER
            );
            INSERT INTO paper_account_v2 (algo, balance, updated_at)
                SELECT 'copy_trade', balance, updated_at
                FROM paper_account WHERE id=1;
            DROP TABLE paper_account;
            ALTER TABLE paper_account_v2 RENAME TO paper_account;
        """)
    elif "updated_at" not in pa_cols and "algo" in pa_cols:
        conn.execute("ALTER TABLE paper_account ADD COLUMN updated_at INTEGER")

    # ── positions PK (market_id, paper) → (market_id, paper, algo) ───────────
    # Only run if the algo column was just added AND the existing PK doesn't
    # include algo. SQLite makes this a table-rebuild.
    pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)") if r[5] > 0]
    if pk_cols == ["market_id", "paper"]:
        conn.executescript("""
            CREATE TABLE positions_v2 (
                market_id        TEXT,
                paper            INTEGER NOT NULL DEFAULT 0,
                algo             TEXT NOT NULL DEFAULT 'copy_trade',
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
            INSERT INTO positions_v2
                SELECT market_id, paper, COALESCE(algo, 'copy_trade'),
                       asset_id, question, outcome, shares, avg_price,
                       total_cost_usdc, opened_at, updated_at
                FROM positions;
            DROP TABLE positions;
            ALTER TABLE positions_v2 RENAME TO positions;
            CREATE INDEX IF NOT EXISTS idx_positions_paper_open
                ON positions(paper, shares);
            CREATE INDEX IF NOT EXISTS idx_positions_algo
                ON positions(algo);
        """)

    conn.commit()
