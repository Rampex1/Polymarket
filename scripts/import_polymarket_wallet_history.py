#!/usr/bin/env python3
"""Seed ranked-copy history from Polymarket's public APIs, without Dune.

Examples:
  python scripts/import_polymarket_wallet_history.py --max-wallets 10
  python scripts/import_polymarket_wallet_history.py --wallet 0xabc... --max-pages-per-wallet 4

The default candidate pool combines all-time P&L, all-time volume, and
monthly P&L leaderboards. It uses conservative binary closed-position labels
for a paper-only seed; use the Dune CSV importer for research or live review.
"""

import argparse
from pathlib import Path
import sys
import time

# `python scripts/...` sets sys.path to scripts/, not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from algorithms.copy_trade.public_history import (
    collect_closed_position_bets,
    leaderboard_wallets,
)
from algorithms.copy_trade.ranker import SQLiteResolvedBetSource
from bot.storage import db


def _store(bets) -> int:
    source = SQLiteResolvedBetSource()
    conn = db.get()
    conn.executescript(source._SCHEMA)
    for bet in bets:
        conn.execute(
            """INSERT OR REPLACE INTO wallet_resolved_bets
               (wallet, entry_price, outcome, resolved_at, copyability_score)
               VALUES (?, ?, ?, ?, ?)""",
            (bet.wallet, bet.entry_price, bet.outcome, bet.resolved_at, bet.copyability_score),
        )
    conn.commit()
    return len(bets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet", action="append", default=[], help="Wallet to import; repeatable.")
    parser.add_argument("--max-wallets", type=int, default=5, help="Maximum leaderboard candidates to inspect.")
    parser.add_argument("--max-pages-per-wallet", type=int, default=2, help="Closed-position pages per wallet (50 rows each).")
    parser.add_argument("--minimum-age-days", type=float, default=30.0, help="Require market end dates to be this far in the past.")
    args = parser.parse_args()
    if args.max_wallets <= 0 or args.max_pages_per_wallet <= 0 or args.minimum_age_days < 0:
        parser.error("wallet/page limits must be positive and --minimum-age-days cannot be negative")

    wallets = [wallet.lower() for wallet in args.wallet] or leaderboard_wallets()
    wallets = wallets[:args.max_wallets]
    if not wallets:
        raise SystemExit("No candidate wallets returned from the public leaderboard.")
    bets = collect_closed_position_bets(
        wallets, args.max_pages_per_wallet, int(args.minimum_age_days * 86_400), int(time.time()),
    )
    print(f"Inspected {len(wallets)} wallets; imported {_store(bets)} conservative paper-history rows.")


if __name__ == "__main__":
    main()
