"""
Price-history archiver — hoard CLOB price data before it evaporates.

Why this exists: the public CLOB `/prices-history` endpoint returns NOTHING
for long-resolved markets (verified June 2026 — a 2025 market yields zero
points at any fidelity). Without our own archive there is no dataset for
copy-execution backtests or insider lead-lag analysis. Every week this
doesn't run is a week of data lost forever.

Storage is a standalone SQLite file (NOT the bot's positions.db — discovery
must never contend with live trading writes). Snapshots are idempotent
upserts, so running it as often as you like only ever adds points.

Usage:
    python -m discovery.archive --once            # cron-friendly single pass
    python -m discovery.archive --loop --every 3600
"""

import argparse
import json
import logging
import os
import sqlite3
import time
from typing import Optional

from bot import fetcher

logger = logging.getLogger(__name__)

def default_db_path() -> str:
    """Archive DB path, read at call time so dotenv loading (which may
    happen after this module is imported) is respected."""
    return os.getenv("DISCOVERY_ARCHIVE_DB", "discovery_archive.db")


# Universe defaults: how many top-volume markets per Gamma source, and the
# firehose notional floor for "whale-touched" markets.
DEFAULT_TOP_N = 50
DEFAULT_MIN_CASH = 10_000.0
RECENT_CLOSED_DAYS = 7

# Delta-fetch overlap: re-request this much already-archived history so a
# point landing between "fetch finished" and "stamp written" is never lost.
# Idempotent upserts make the re-fetch free apart from a little bandwidth.
SNAPSHOT_OVERLAP_SECONDS = 2 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    token_id  TEXT    NOT NULL,
    fidelity  INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    price     REAL    NOT NULL,
    PRIMARY KEY (token_id, fidelity, ts)
);

CREATE TABLE IF NOT EXISTS tracked_markets (
    condition_id   TEXT PRIMARY KEY,
    token_ids      TEXT NOT NULL,          -- JSON list of CLOB token ids
    question       TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    first_seen     INTEGER NOT NULL,
    last_snapshot  INTEGER
);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open (and if needed create) the archive DB. WAL for concurrent reads."""
    conn = sqlite3.connect(db_path or default_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Storage primitives
# ---------------------------------------------------------------------------


def upsert_history(
    conn: sqlite3.Connection,
    token_id: str,
    fidelity: int,
    points: list[dict],
) -> int:
    """Insert price points, ignoring ones already stored. Returns rows added."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO price_history (token_id, fidelity, ts, price) "
        "VALUES (?, ?, ?, ?)",
        [(token_id, fidelity, int(p["t"]), float(p["p"])) for p in points],
    )
    conn.commit()
    return conn.total_changes - before


def track_market(conn: sqlite3.Connection, market: dict, source: str) -> bool:
    """Register a market for snapshotting. First registration wins (the
    original `source` and `first_seen` are kept). Returns True if added."""
    condition_id = market.get("conditionId")
    if not condition_id:
        return False

    raw_tokens = market.get("clobTokenIds") or "[]"
    try:
        token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
    except ValueError:
        logger.debug("Unparseable clobTokenIds for %s: %r", condition_id, raw_tokens)
        return False
    if not token_ids:
        return False

    before = conn.total_changes
    conn.execute(
        "INSERT OR IGNORE INTO tracked_markets "
        "(condition_id, token_ids, question, source, first_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            condition_id,
            json.dumps(list(token_ids)),
            market.get("question", ""),
            source,
            int(time.time()),
        ),
    )
    conn.commit()
    return conn.total_changes > before


# ---------------------------------------------------------------------------
# Universe collection — what's worth archiving
# ---------------------------------------------------------------------------


def collect_universe(
    conn: sqlite3.Connection,
    min_cash: float = DEFAULT_MIN_CASH,
    top_n: int = DEFAULT_TOP_N,
) -> int:
    """Union three sources into tracked_markets; returns newly-added count.

    1. Active top-volume markets (where the action is now).
    2. Recently-closed top-volume markets (grab their history before the
       CLOB drops it).
    3. Whale-touched markets from the trade firehose (anything a $10k+
       trade landed in is a market a future backtest will ask about).
    """
    added = 0

    for market in fetcher.fetch_top_markets(closed=False, limit=top_n):
        added += track_market(conn, market, source="gamma_active")

    end_date_min = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - RECENT_CLOSED_DAYS * 86_400),
    )
    for market in fetcher.fetch_top_markets(
        closed=True, limit=top_n, end_date_min=end_date_min,
    ):
        added += track_market(conn, market, source="gamma_recent_closed")

    # Whale-flow rows carry only the token the whale traded; archiving just
    # that side leaves the complementary outcome's series unrecorded. Pull
    # the full market from Gamma for each NEW market (already-tracked ones
    # are skipped first so hot markets don't cost a lookup every pass), and
    # fall back to the traded token if Gamma can't be reached — half a
    # series beats none.
    tracked_ids = {
        row[0] for row in conn.execute("SELECT condition_id FROM tracked_markets")
    }
    for trade in fetcher.fetch_global_trades(min_cash):
        if trade.market_id in tracked_ids:
            continue
        market = fetcher.fetch_market_resolution(trade.market_id)
        if not (market and market.get("clobTokenIds")):
            market = {
                "conditionId": trade.market_id,
                "question": trade.title,
                "clobTokenIds": json.dumps([trade.asset_id]),
            }
        if track_market(conn, market, source="whale_flow"):
            added += 1
            tracked_ids.add(trade.market_id)

    return added


# ---------------------------------------------------------------------------
# Snapshotting
# ---------------------------------------------------------------------------


def snapshot_all(conn: sqlite3.Connection, fidelity: int = 60) -> dict:
    """Fetch + store price history for every tracked token.

    First pass per market pulls the full history (interval=max); after a
    successful pass, only the window since `last_snapshot` (minus a small
    overlap) is fetched — without the delta, every pass re-downloads months
    of data for every token and the archive's bandwidth grows without bound.

    A failed fetch (None) skips that token and keeps going — one flaky token
    must not stall the archive. Empty history ([]) is recorded as success:
    it's the API's real answer for dead markets.
    """
    tokens_seen = 0
    points_added = 0
    failures = 0

    rows = conn.execute(
        "SELECT condition_id, token_ids, last_snapshot FROM tracked_markets",
    ).fetchall()

    for condition_id, raw_tokens, last_snapshot in rows:
        start_ts = (
            int(last_snapshot) - SNAPSHOT_OVERLAP_SECONDS
            if last_snapshot else None
        )
        market_ok = True
        for token_id in json.loads(raw_tokens):
            tokens_seen += 1
            points = fetcher.fetch_price_history(
                token_id, fidelity=fidelity, start_ts=start_ts,
            )
            if points is None:
                failures += 1
                market_ok = False
                continue
            points_added += upsert_history(conn, token_id, fidelity, points)

        if market_ok:
            conn.execute(
                "UPDATE tracked_markets SET last_snapshot = ? WHERE condition_id = ?",
                (int(time.time()), condition_id),
            )
            conn.commit()

    return {"tokens": tokens_seen, "points_added": points_added, "failures": failures}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_once(
    db_path: Optional[str] = None,
    min_cash: float = DEFAULT_MIN_CASH,
    top_n: int = DEFAULT_TOP_N,
    fidelity: int = 60,
) -> dict:
    """One collect + snapshot pass. Returns a summary dict (cron-loggable)."""
    conn = connect(db_path)
    try:
        tracked_added = collect_universe(conn, min_cash=min_cash, top_n=top_n)
        summary = snapshot_all(conn, fidelity=fidelity)
        summary["tracked_added"] = tracked_added
        logger.info("Archive pass: %s", summary)
        return summary
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", default=None,
                        help="archive sqlite path (default: $DISCOVERY_ARCHIVE_DB "
                             "or discovery_archive.db)")
    parser.add_argument("--once", action="store_true", help="single pass (cron)")
    parser.add_argument("--loop", action="store_true", help="run forever")
    parser.add_argument("--every", type=int, default=3600,
                        help="seconds between loop passes")
    parser.add_argument("--min-cash", type=float, default=DEFAULT_MIN_CASH)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--fidelity", type=int, default=60)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.loop:
        while True:
            try:
                run_once(args.db, args.min_cash, args.top_n, args.fidelity)
            except Exception:
                logger.exception("Archive pass failed; retrying next cycle")
            time.sleep(args.every)
    else:
        summary = run_once(args.db, args.min_cash, args.top_n, args.fidelity)
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
