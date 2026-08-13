"""Phase 3 cohort builder — discover wallets, reconstruct their record, rank.

    python -m algorithms.copy_trade.cohort --discover 40      # widen the pool, then rank
    python -m algorithms.copy_trade.cohort                    # re-rank what we already have
    python -m algorithms.copy_trade.cohort --activate copy_trade_paper

Batch on purpose, not a worker thread. The ranker consumes *resolved* bets, so
its input only changes as markets settle — days to weeks. Polling faster buys
nothing, while a job that crawls hundreds of wallets wants to be restartable
and re-runnable against a filled table when you sweep the thresholds.

`--activate <algo>` writes the ranked cohort to the watchlist for that
algorithm name. `ConsensusEngine` reads `active_wallets()` on every snapshot,
so a newly activated cohort is picked up within one snapshot interval with no
redeploy. Without the flag nothing is written and this only prints.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor

from bot.polymarket import api

from .history import resolved_bets_from
from .params import CopyTradeParams
from .ranker import (
    SQLiteResolvedBetSource, known_wallets, persistence_passes, rank_wallets,
    store_resolved_bets,
)
from .watchlist import WatchlistRepository

ACTIVITY_PAGE = 500
POSITION_PAGE = 500
POSITION_PAGES = 12       # 6000 positions; past that a wallet's tail is noise


def discover(count: int, min_cash: float) -> list[str]:
    """Wallets currently moving size on the firehose.

    Selects for "active and large right now", which is not the same as "good"
    — that is what the ranking below is for. Repeated runs widen the pool
    because ingested wallets persist in `wallet_resolved_bets`.
    """
    trades = api.fetch_global_trades(min_cash_usdc=min_cash, limit=500)
    seen, out = set(), []
    for t in sorted(trades, key=lambda t: t.cash_usdc, reverse=True):
        w = t.wallet.lower()
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:count]


def ingest(wallet: str, max_pages: int) -> tuple[int, bool]:
    """Reconstruct one wallet's resolved record.

    Returns (rows written, whether the crawl hit the page cap). Every bet is
    anchored to a BUY in the crawled window, so a capped crawl just means a
    shorter sample — not a skewed one.
    """
    activity: list[dict] = []
    truncated = True
    for page in range(min(max_pages, api.ACTIVITY_MAX_ROWS // ACTIVITY_PAGE)):
        rows = api.fetch_activity(wallet, limit=ACTIVITY_PAGE, offset=page * ACTIVITY_PAGE)
        # None is a failed read, not an empty tail — mistaking the two would
        # report a partial crawl as a wallet's complete record.
        if rows is None:
            break
        activity.extend(rows)
        if len(rows) < ACTIVITY_PAGE:
            truncated = False
            break
    # Positions cap at 500 a page. Wins come from up to 5500 activity rows, so
    # reading one page of losses would hand the ranker a wallet that mostly
    # wins — unredeemed losers are exactly what accumulates past the cap.
    positions: list[dict] = []
    for page in range(POSITION_PAGES):
        rows = api.fetch_user_positions(wallet, limit=POSITION_PAGE, offset=page * POSITION_PAGE)
        positions.extend(rows)
        if len(rows) < POSITION_PAGE:
            break

    bets = resolved_bets_from(wallet, activity, positions)
    return store_resolved_bets(bets), truncated


def main() -> int:
    d = CopyTradeParams()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--discover", type=int, metavar="N", help="pull N new candidates off the firehose first")
    p.add_argument("--discover-min-cash", type=float, default=5_000.0)
    p.add_argument("--max-pages", type=int, default=11,
                   help=f"activity pages per wallet (×{ACTIVITY_PAGE} rows); "
                        f"the endpoint stops serving past {api.ACTIVITY_MAX_ROWS} rows")
    p.add_argument("--min-bets", type=int, default=d.watchlist_min_resolved_bets)
    p.add_argument("--confidence-z", type=float, default=d.watchlist_confidence_z)
    p.add_argument("--min-copyability", type=float, default=d.watchlist_min_copyability_score)
    p.add_argument("--top", type=int, default=20, help="cohort size to report and activate")
    p.add_argument("--activate", metavar="ALGO", help="write the cohort to this algorithm's watchlist")
    args = p.parse_args()

    pool = known_wallets()
    if args.discover:
        fresh = [w for w in discover(args.discover, args.discover_min_cash) if w not in set(pool)]
        print(f"Discovered {len(fresh)} new wallets ({len(pool)} already known).")
        if fresh:
            print(f"Reconstructing history (≤{args.max_pages} pages each)…")
            with ThreadPoolExecutor(max_workers=4) as ex:
                results = list(ex.map(lambda w: ingest(w, args.max_pages), fresh))
            capped = sum(1 for _, t in results if t)
            print(f"Wrote {sum(n for n, _ in results)} resolved bets.")
            if capped:
                print(f"NOTE: {capped}/{len(fresh)} wallets hit the {args.max_pages}-page "
                      f"cap, so only their recent bets are sampled. The endpoint "
                      f"stops at {api.ACTIVITY_MAX_ROWS} rows, so heavy traders are "
                      f"capped no matter what.")
            print()
        pool = known_wallets()

    if not pool:
        print("No history yet. Run with --discover N to build the pool.")
        return 1

    bets = SQLiteResolvedBetSource().resolved_bets(pool)
    scores = rank_wallets(bets, args.min_bets, args.confidence_z, args.min_copyability)
    passed = persistence_passes(bets, args.min_bets, args.confidence_z,
                                args.min_copyability, args.top)

    print(f"Pool: {len(pool)} wallets, {len(bets)} resolved bets. "
          f"{len(scores)} clear the {args.min_bets}-bet bar.")
    print(f"Persistence test (early winners still winning late): "
          f"{'PASS' if passed else 'FAIL'}\n")

    if scores:
        print(f"{'N':>4} {'MEAN EDGE':>10} {'LOWER':>8}  WALLET")
        print("-" * 52)
        for s in scores[:args.top]:
            print(f"{s.sample_size:>4} {s.mean_edge:>+10.3f} {s.edge_lower_bound:>+8.3f}  {s.wallet}")
        print("\nLOWER is the confidence-bound edge — the number to rank on. A big "
              "MEAN on a small N is mostly luck.")

    if args.activate:
        repo = WatchlistRepository()
        repo.refresh(args.activate, bets, watchlist_size=args.top,
                     min_resolved_bets=args.min_bets, confidence_z=args.confidence_z,
                     min_copyability_score=args.min_copyability,
                     persistence_passed=passed)
        active = repo.active_wallets(args.activate)
        print(f"\nActivated {len(active)} wallets for '{args.activate}'."
              if active else
              f"\nActivated nothing for '{args.activate}' — the watchlist only "
              f"opens when the persistence test passes, and it did not.")
    elif scores:
        print("\nNothing written. Pass --activate <algo> to make this the live cohort.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
