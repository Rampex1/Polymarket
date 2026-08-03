#!/usr/bin/env python3
"""Import normalized resolved-wallet history exported from Dune.

Required CSV columns: wallet,entry_price,outcome,resolved_at
Optional: copyability_score (defaults to 1.0)
"""

import argparse
import csv

from bot import db
from algorithms.copy_trade.ranker import SQLiteResolvedBetSource


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    args = parser.parse_args()
    source = SQLiteResolvedBetSource()
    conn = db.get()
    conn.executescript(source._SCHEMA)
    inserted = 0
    with open(args.csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            conn.execute(
                """INSERT OR REPLACE INTO wallet_resolved_bets
                   (wallet, entry_price, outcome, resolved_at, copyability_score)
                   VALUES (?, ?, ?, ?, ?)""",
                (row["wallet"].lower(), float(row["entry_price"]),
                 float(row["outcome"]), int(row["resolved_at"]),
                 float(row.get("copyability_score") or 1.0)),
            )
            inserted += 1
    conn.commit()
    print(f"Imported {inserted} resolved bets.")


if __name__ == "__main__":
    main()
