"""Seed the price archive with the markets resolution_carry needs.

    python -m algorithms.resolution_carry.seed --within-days 7
    python -m algorithms.resolution_carry.seed --within-days 3 --sports-only

The archiver (`algorithms.insider_flow.archive`) snapshots whatever sits in
`tracked_markets`; what goes in there is a separate question, and its own
universe — top volume plus whale flow — is not this strategy's universe.

We need the price *path into* resolution, not a snapshot of markets already
priced at 0.98. A market sitting in the band today will be gone tomorrow, and
a game that ends decisively passes through 0.93 -> 1.00 in its final minutes.
So the rule here is **anything resolving soon**, regardless of today's price:
that captures the whole approach to settlement, which is what a calibration
backtest has to read.

Writes only to `tracked_markets`, never to `price_history`, and uses
INSERT OR IGNORE — running it repeatedly only ever adds.
"""

import argparse
import time

from algorithms.insider_flow.archive import connect, track_market
from bot.polymarket import api

GAMMA_PAGE = 100          # /markets caps a page here regardless of `limit`
MAX_PAGES = 40


def open_markets(max_pages: int = MAX_PAGES) -> list[dict]:
    """Open markets, most-traded first."""
    out: list[dict] = []
    for page in range(max_pages):
        try:
            resp = api.SESSION.get(
                f"{api.config.GAMMA_API}/markets",
                params={"closed": "false", "order": "volumeNum", "ascending": "false",
                        "limit": GAMMA_PAGE, "offset": page * GAMMA_PAGE},
                timeout=20,
            )
            # Gamma 422s past its offset ceiling rather than returning an
            # empty page — that is the end of the list, not a failure.
            if resp.status_code == 422:
                break
            resp.raise_for_status()
            rows = resp.json()
        except Exception as e:
            print(f"  page {page} failed: {e}")
            break
        out.extend(rows)
        if len(rows) < GAMMA_PAGE:
            break
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=None, help="archive path (default: the shared one)")
    p.add_argument("--within-days", type=float, default=7.0,
                   help="track markets resolving within this many days (default 7)")
    p.add_argument("--min-ask", type=float, default=0.0,
                   help="also require this ask floor; 0 tracks the whole path in")
    p.add_argument("--sports-only", action="store_true",
                   help="restrict to Gamma sports/esports categories")
    p.add_argument("--limit", type=int, default=400,
                   help="cap markets added per run — every one costs a CLOB call each pass")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    rows = open_markets()
    now = time.time()
    horizon = now + args.within_days * 86_400

    picked, sports = [], 0
    for m in rows:
        end_ts = api.market_end_ts(m)
        if end_ts is None or not (now < end_ts <= horizon):
            continue
        try:
            ask = float(m.get("bestAsk") or 0)
        except (TypeError, ValueError):
            continue
        if ask < args.min_ask:
            continue
        # One Gamma call per market, and only for those already past the
        # cheap gates — the label lookup is the expensive part of this job.
        is_sport = "sports" in api.market_labels(m)
        if args.sports_only and not is_sport:
            continue
        sports += is_sport
        picked.append(m)
        if len(picked) >= args.limit:
            break

    print(f"{len(rows)} open markets scanned → {len(picked)} resolving within "
          f"{args.within_days:g}d ({sports} sports).")
    if args.dry_run:
        for m in picked[:15]:
            hrs = (api.market_end_ts(m) - now) / 3600
            print(f"  {hrs:>7.1f}h  ask={float(m.get('bestAsk') or 0):.3f}  {m.get('question','')[:56]}")
        return 0

    conn = connect(args.db)
    added = sum(track_market(conn, m, source="resolution_carry") for m in picked)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM tracked_markets").fetchone()[0]
    print(f"Added {added} new markets. {total} now tracked.")
    print("The archiver picks these up on its next pass; it must be running "
          "for any of this to become data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
