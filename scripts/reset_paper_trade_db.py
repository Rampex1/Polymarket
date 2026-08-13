#!/usr/bin/env python3
"""
Reset paper trading state.

    python scripts/reset_paper_trade_db.py --algo resolution_carry_paper
    python scripts/reset_paper_trade_db.py                  # the whole DB

Reach for `--algo` by default. One SQLite file holds every algorithm's rows,
partitioned by `algo`, so the unscoped form is not "reset the strategy I am
working on" — it deletes the trade history of every algorithm that ever ran
under this database, including live ones. That history is the only record
there is; the exchange will not reissue it.

Hence the guard: an unscoped wipe refuses to run when the database holds
real-money rows (paper=0) unless `--force` says that is genuinely intended.
"""

import argparse
import os
import sqlite3
import sys

# Allow running from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algorithms import ENABLED
from bot import config
from bot.storage.ledger import Ledger

# Every table partitioned by `algo`. discord_threads is included so a reset
# market opens a fresh thread instead of nesting under the old one.
ALGO_TABLES = (
    "positions", "trade_log", "daily_stats", "paper_account",
    "position_lots", "signals", "discord_threads", "runs",
)


def live_row_count(db_path: str) -> int:
    """Real-money rows in the database, across positions and trade_log."""
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        total = 0
        for table in ("positions", "trade_log"):
            try:
                total += conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE paper=0"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pass          # table not created yet
        return total
    finally:
        conn.close()


def reset_one(db_path: str, algo: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for table in ALGO_TABLES:
                try:
                    deleted = conn.execute(
                        f"DELETE FROM {table} WHERE algo = ?", (algo,)
                    ).rowcount
                except sqlite3.OperationalError:
                    continue  # table not created yet
                if deleted:
                    print(f"  {table:18} {deleted} row(s) deleted")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--algo", help="reset only this algorithm's rows (its DB partition key)")
    ap.add_argument("--force", action="store_true",
                    help="allow an unscoped wipe even when real-money rows exist")
    args = ap.parse_args()

    db_path = config.DB_PATH
    enabled = {a.params.name: a.params.paper_starting_balance for a in ENABLED}

    if args.algo:
        if args.algo not in enabled:
            print(f"'{args.algo}' is not enabled in this profile. "
                  f"Enabled: {', '.join(sorted(enabled)) or 'none'}.")
            return 1
        print(f"Resetting [{args.algo}] in {db_path}")
        reset_one(db_path, args.algo)
        ledger = Ledger(algo=args.algo)
        ledger.init_paper_balance(enabled[args.algo])
        print(f"Fresh paper account [{args.algo}] with ${enabled[args.algo]:.2f}")
        return 0

    live = live_row_count(db_path)
    if live and not args.force:
        print(
            f"REFUSING: {db_path} holds {live} real-money row(s) (paper=0), and\n"
            f"deleting the file would destroy the only record of those trades.\n"
            f"Reset one algorithm with --algo <name>, or pass --force if you\n"
            f"really do mean to discard live history."
        )
        return 1

    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"Deleted {db_path}")

    for name, balance in enabled.items():
        Ledger(algo=name).init_paper_balance(balance)
        print(f"Fresh paper account [{name}] created with ${balance:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
