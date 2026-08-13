"""Is a market priced at 0.98 actually right 98% of the time?

    python -m algorithms.resolution_carry.calibration
    python -m algorithms.resolution_carry.calibration --sports-only --min-price 0.90

The premise of resolution_carry, tested against archived prices rather than
assumed. Reads `data/discovery_archive.db` — real market prices over time —
and joins each token to its final outcome via Gamma. Nothing here touches the
trading path or places an order.

Two questions, both of which have to come out right before this strategy
deserves capital:

  1. **Calibration.** Bucketed by price and by hours remaining, what fraction
     resolved YES? A calibrated market gives edge ~0, which means carry earns
     nothing. The strategy needs favourites to be *underpriced*.
  2. **Staleness.** How far does a market at 0.95+ move in the next few
     minutes? That is the adverse-selection cost of polling on an interval,
     and it decides whether an in-play sports universe is tradeable at all —
     a price we act on is only as good as it is fresh.

Two measurement traps this is careful about:

  * **Autocorrelation.** A market sitting at 0.98 for three hours emits ~180
     minute bars, which is one observation, not 180. Rows are therefore
     reduced to one per (token, horizon band), and `n` counts tokens.
  * **Scheduled vs actual end.** Horizons use the market's *scheduled* end
     date, because that is all a live strategy would know at entry time.
"""

import argparse
import json
import sqlite3
import statistics as st
from bisect import bisect_left
from typing import Optional

from algorithms.insider_flow.archive import connect
from bot.polymarket import api

PRICE_BANDS = [(0.99, 1.01), (0.98, 0.99), (0.95, 0.98),
               (0.90, 0.95), (0.80, 0.90), (0.50, 0.80)]
HOUR_BANDS = [(0, 6), (6, 24), (24, 72), (72, 336), (336, 1e9)]
HOUR_LABELS = {(0, 6): "< 6h", (6, 24): "6-24h", (24, 72): "1-3d",
               (72, 336): "3-14d", (336, 1e9): "> 14d"}


def token_outcomes(conn: sqlite3.Connection) -> dict[str, tuple[float, float, bool]]:
    """token_id → (outcome, scheduled end ts, is_sport) for resolved markets."""
    out: dict[str, tuple[float, float, bool]] = {}
    rows = conn.execute("SELECT condition_id, token_ids FROM tracked_markets").fetchall()
    for i, (cond, token_json) in enumerate(rows, 1):
        if i % 25 == 0:
            print(f"  resolving {i}/{len(rows)}…", flush=True)
        market = api.fetch_market_resolution(cond)
        if not market or not api.market_outcome_is_final(market):
            continue
        end_ts = api.market_end_ts(market)
        if end_ts is None:
            continue
        try:
            prices = [float(x) for x in json.loads(market.get("outcomePrices") or "[]")]
            tokens = json.loads(token_json)
        except (ValueError, TypeError):
            continue
        is_sport = "sports" in api.market_labels(market)
        for idx, token in enumerate(tokens):
            if idx < len(prices):
                out[str(token)] = (prices[idx], end_ts, is_sport)
    return out


def observations(
    conn: sqlite3.Connection, outcomes: dict, sports_only: bool = False,
) -> list[tuple[float, float, float, bool]]:
    """One (price, hours_left, outcome, is_sport) per token per horizon band.

    Collapsing to one row per band is the whole point: consecutive minute bars
    from one market are the same observation seen repeatedly, and counting
    them as independent would shrink every confidence interval by ~13x.
    """
    picked: dict[tuple[str, int], tuple[float, float, float, bool]] = {}
    for token, ts, price in conn.execute(
        "SELECT token_id, ts, price FROM price_history ORDER BY ts"
    ):
        entry = outcomes.get(str(token))
        if entry is None:
            continue
        outcome, end_ts, is_sport = entry
        if sports_only and not is_sport:
            continue
        hours = (end_ts - ts) / 3600
        if hours <= 0:
            continue
        for slot, (lo, hi) in enumerate(HOUR_BANDS):
            if lo <= hours < hi:
                # Last point in the band: the freshest price at that horizon.
                picked[(str(token), slot)] = (float(price), hours, outcome, is_sport)
                break
    return list(picked.values())


def calibrate(obs: list, min_price: float) -> list[dict]:
    """Resolved-YES rate against price paid, bucketed by price and horizon."""
    out = []
    for plo, phi in PRICE_BANDS:
        if phi <= min_price:
            continue
        for hlo, hhi in HOUR_BANDS:
            rows = [o for o in obs if plo <= o[0] < phi and hlo <= o[1] < hhi]
            if not rows:
                continue
            priced = st.mean(r[0] for r in rows)
            won = st.mean(r[2] for r in rows)
            out.append({
                "price_band": f"{plo:.2f}-{min(phi, 1.0):.2f}",
                "hours": HOUR_LABELS[(hlo, hhi)],
                "n": len(rows), "priced": priced, "won": won,
                "edge": won - priced,
            })
    return out


def staleness(
    conn: sqlite3.Connection, outcomes: dict, min_price: float,
    horizons_min: tuple[int, ...] = (1, 5, 15, 60),
) -> list[dict]:
    """How far a rich market moves over the next few minutes.

    This is the cost of acting on an interval-polled price. A median move that
    rivals the whole 2% return means the fills you get are the ones that moved
    against you, and the universe is not tradeable on that poll cadence.
    """
    series: dict[str, list[tuple[int, float]]] = {}
    for token, ts, price in conn.execute(
        "SELECT token_id, ts, price FROM price_history ORDER BY token_id, ts"
    ):
        if str(token) in outcomes:
            series.setdefault(str(token), []).append((int(ts), float(price)))

    out = []
    for minutes in horizons_min:
        moves = []
        window = minutes * 60
        # Bars land on an irregular grid (46-53s apart in practice), so the
        # later point is found by seeking past the target and accepting the
        # first bar within half a horizon of it. Exact-timestamp lookup finds
        # almost nothing.
        tolerance = max(45, window // 2)
        for points in series.values():
            stamps = [t for t, _ in points]
            for i, (ts, price) in enumerate(points):
                if price < min_price:
                    continue
                target = ts + window
                j = bisect_left(stamps, target, lo=i)
                # Take whichever of the bars either side of the target is
                # nearest: on a 53s grid the preceding bar is frequently the
                # closer one, and seeking only forward misses it.
                best = min(
                    (c for c in (j - 1, j) if i < c < len(points)),
                    key=lambda c: abs(stamps[c] - target), default=None,
                )
                if best is not None and abs(stamps[best] - target) <= tolerance:
                    moves.append(abs(points[best][1] - price))
        if moves:
            moves.sort()
            out.append({
                "minutes": minutes, "n": len(moves),
                "median": st.median(moves),
                "p95": moves[int(len(moves) * 0.95)],
                "worst": moves[-1],
            })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=None)
    p.add_argument("--min-price", type=float, default=0.80,
                   help="ignore bands below this (default 0.80)")
    p.add_argument("--sports-only", action="store_true")
    p.add_argument("--staleness-min-price", type=float, default=0.95)
    args = p.parse_args()

    conn = connect(args.db)
    print("Resolving archived markets against Gamma…")
    outcomes = token_outcomes(conn)
    obs = observations(conn, outcomes, sports_only=args.sports_only)
    tokens = {o for o in outcomes}
    print(f"\n{len(tokens)} resolved tokens, {len(obs)} independent observations"
          f"{' (sports only)' if args.sports_only else ''}.\n")

    rows = calibrate(obs, args.min_price)
    if not rows:
        print("No observations in the requested bands yet. The archiver needs "
              "to run until seeded markets resolve — check back in a week.")
        return 1

    print(f"CALIBRATION — does the price tell the truth?")
    print(f"{'price band':>12} {'horizon':>8} {'n':>5} {'priced':>8} {'resolved':>9} {'edge':>8}")
    print("-" * 56)
    for r in rows:
        print(f"{r['price_band']:>12} {r['hours']:>8} {r['n']:>5} "
              f"{r['priced']:>8.3f} {r['won']:>9.3f} {r['edge']:>+8.3f}")
    print("\nedge = resolved − priced. Zero means the market is calibrated and "
          "carry earns nothing; the strategy needs it positive.")

    stale = staleness(conn, outcomes, args.staleness_min_price)
    if stale:
        print(f"\nSTALENESS — move of a market already at {args.staleness_min_price:.2f}+")
        print(f"{'after':>8} {'n':>7} {'median':>9} {'p95':>9} {'worst':>9}")
        print("-" * 46)
        for s in stale:
            print(f"{s['minutes']:>6}m {s['n']:>7} {s['median']:>9.4f} "
                  f"{s['p95']:>9.4f} {s['worst']:>9.4f}")
        print("\nCompare these to the ~2% a 0.98 entry pays. A p95 move that "
              "rivals the return is the adverse-selection cost of polling on "
              "an interval — the fills you get are the ones that moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
