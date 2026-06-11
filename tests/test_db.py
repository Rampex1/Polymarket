"""
DB layer tests — schema, migration, pragmas, indexes.

We hit the real SQLite engine via a tempfile to catch issues that a mock
would hide (e.g. WAL mode pragma syntax, column migration idempotence).
"""

import sqlite3


def test_schema_creates_all_tables(fresh_db):
    conn = fresh_db.get()
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"positions", "trade_log", "daily_stats", "paper_account"} <= tables


def test_indexes_exist(fresh_db):
    conn = fresh_db.get()
    idx = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # Hot-path indexes the bot relies on for performance.
    assert "idx_trade_log_paper_ts" in idx
    assert "idx_trade_log_market" in idx
    assert "idx_positions_paper_open" in idx


def test_wal_mode_enabled(fresh_db):
    conn = fresh_db.get()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_migration_is_idempotent(fresh_db, tmp_db_path):
    """Re-initializing against an existing DB shouldn't blow up."""
    fresh_db.get()
    # Force a re-init by closing the cached connection.
    fresh_db.reset_for_tests()
    conn = fresh_db.get()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    assert "fee_usdc" in cols
    assert "realized_pnl" in cols


def test_migration_adds_fee_usdc_on_legacy_schema(tmp_db_path, monkeypatch):
    """A DB created with the older schema should get fee_usdc added in place."""
    from bot import config
    import bot.db as dbmod

    # Hand-build a legacy trade_log without fee_usdc, outcome, question.
    conn = sqlite3.connect(tmp_db_path)
    conn.executescript(
        """
        CREATE TABLE trade_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id    TEXT,
            asset_id     TEXT,
            action       TEXT,
            shares       REAL,
            price        REAL,
            usdc_amount  REAL,
            realized_pnl REAL DEFAULT 0,
            paper        INTEGER DEFAULT 0,
            ts           INTEGER
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", tmp_db_path)
    dbmod.reset_for_tests()
    conn = dbmod.get()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_log)")}
    assert "fee_usdc" in cols
    assert "outcome" in cols
    assert "question" in cols


def test_threadlocal_connection_isolation(fresh_db):
    """Each thread must get its own connection. Two threads getting the same
    object would mean WAL gives no benefit and writes can interleave."""
    import threading

    main_conn = fresh_db.get()
    other_conn = []

    def get_in_thread():
        other_conn.append(fresh_db.get())

    t = threading.Thread(target=get_in_thread)
    t.start()
    t.join()

    assert other_conn[0] is not main_conn


# ---------------------------------------------------------------------------
# data/ directory — default paths + auto-created parents
# ---------------------------------------------------------------------------


def test_get_creates_missing_parent_dirs(tmp_path, monkeypatch):
    """sqlite3.connect fails on a missing directory; db.get() must create it
    so the data/ default works on a fresh checkout."""
    from bot import config
    import bot.db as dbmod

    nested = tmp_path / "data" / "deep" / "pos.db"
    monkeypatch.setattr(config, "DB_PATH", str(nested))
    dbmod.reset_for_tests()
    try:
        dbmod.get()
        assert nested.exists()
    finally:
        dbmod.reset_for_tests()


def test_default_db_path_prefers_data_dir(tmp_path, monkeypatch):
    import os

    from bot import config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_PATH", raising=False)
    assert config._default_db_path() == os.path.join("data", "positions.db")


def test_default_db_path_env_override_wins(tmp_path, monkeypatch):
    from bot import config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_PATH", "/elsewhere/x.db")
    assert config._default_db_path() == "/elsewhere/x.db"


def test_default_db_path_legacy_fallback(tmp_path, monkeypatch):
    """A pre-existing ./positions.db with no data/ copy must keep being used —
    a deploy that git-pulls this change must not silently start a fresh DB."""
    from bot import config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_PATH", raising=False)
    (tmp_path / "positions.db").touch()
    assert config._default_db_path() == "positions.db"
