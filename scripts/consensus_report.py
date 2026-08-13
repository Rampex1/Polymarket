#!/usr/bin/env python3
"""Phase 0 consensus reporter — READ-ONLY. Places no orders, writes no DB.

    python scripts/consensus_report.py cohort.txt
    python scripts/consensus_report.py --from-firehose 40 --save cohort.txt

`cohort.txt` is one proxy-wallet address per line; blank lines and `#`
comments are ignored.

`--from-firehose N` bootstraps a cohort from wallets currently trading size.
That is somewhere to *start looking*, not a ranked list — it selects for
"active and large right now", which is not the same as "good". Curate the
file by hand afterwards.
"""

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

# Allow running from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algorithms.copy_trade.consensus import find_consensus, parse_positions
from bot.polymarket import api


def load_cohort(path: str) -> list[str]:
    with open(path) as fh:
        lines = [line.split("#")[0].strip().lower() for line in fh]
    return [w for w in lines if w.startswith("0x")]


def bootstrap_cohort(count: int, min_cash: float) -> list[str]:
    """Distinct wallets from the trade firehose, biggest tickets first."""
    trades = api.fetch_global_trades(min_cash_usdc=min_cash, limit=500)
    seen, out = set(), []
    for t in sorted(trades, key=lambda t: t.cash_usdc, reverse=True):
        w = t.wallet.lower()
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:count]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cohort", nargs="?", help="file of proxy-wallet addresses, one per line")
    p.add_argument("--from-firehose", type=int, metavar="N", help="bootstrap a cohort of N active wallets")
    p.add_argument("--firehose-min-cash", type=float, default=5_000.0)
    p.add_argument("--save", metavar="FILE", help="write the bootstrapped cohort here for hand-curation")
    p.add_argument("--min-support", type=int, default=3, help="distinct wallets on one side (default 3)")
    p.add_argument("--min-margin", type=int, default=2, help="support minus opposition (default 2)")
    p.add_argument("--min-conviction", type=float, default=0.0,
                   help="position must be this fraction of the wallet's deployed capital")
    p.add_argument("--max-price", type=float, default=0.97,
                   help="drop markets already priced as decided (default 0.97)")
    p.add_argument("--exclude", metavar="REGEX", default="",
                   help="drop markets whose title matches, e.g. 'FIFA|NBA|vs\\.'")
    p.add_argument("-v", "--verbose", action="store_true", help="list the wallets behind each row")
    args = p.parse_args()

    if args.from_firehose:
        cohort = bootstrap_cohort(args.from_firehose, args.firehose_min_cash)
        if args.save:
            with open(args.save, "w") as fh:
                fh.write("# bootstrapped from the trade firehose — curate by hand\n")
                fh.write("\n".join(cohort) + "\n")
            print(f"Wrote {len(cohort)} wallets to {args.save}\n")
    elif args.cohort:
        cohort = load_cohort(args.cohort)
    else:
        p.error("give a cohort file or --from-firehose N")

    if not cohort:
        print("Empty cohort.")
        return 1

    print(f"Snapshotting {len(cohort)} wallets…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(api.fetch_user_positions, cohort))

    holdings = []
    for wallet, rows in zip(cohort, snapshots):
        holdings.extend(parse_positions(wallet, rows))
    live = {h.wallet for h in holdings}
    print(f"{len(holdings)} live positions across {len(live)} wallets "
          f"({len(cohort) - len(live)} had none or failed to fetch).\n")

    rows = find_consensus(
        holdings, min_support=args.min_support, min_margin=args.min_margin,
        min_conviction=args.min_conviction, max_price=args.max_price,
    )
    if args.exclude:
        pattern = re.compile(args.exclude, re.I)
        kept = [c for c in rows if not pattern.search(c.title)]
        print(f"--exclude dropped {len(rows) - len(kept)} of {len(rows)} rows.\n")
        rows = kept

    if not rows:
        print("No consensus at these thresholds.")
        return 0

    print(f"{'SUP':>3} {'OPP':>3} {'COHORT $':>11} {'ENTRY':>6} {'NOW':>6} "
          f"{'DRIFT':>7}  {'ENDS':<10} MARKET")
    print("-" * 108)
    for c in rows:
        print(f"{c.support:>3} {c.opposition:>3} {c.cohort_cost_usdc:>11,.0f} "
              f"{c.avg_entry:>6.3f} {c.current_price:>6.3f} {c.drift:>+6.0%}  "
              f"{c.end_date[:10]:<10} {c.title[:52]} ({c.outcome})")
        if args.verbose:
            print(f"{'':>4}{', '.join(w[:10] + '…' for w in c.wallets)}")
    print(f"\n{len(rows)} consensus markets. DRIFT is how far price ran past "
          f"the cohort's cost basis — high means you missed it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
