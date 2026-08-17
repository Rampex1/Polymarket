"""Forward-test a forecaster against the market price.

    python -m algorithms.forecast_edge.harness --ask 40
    python -m algorithms.forecast_edge.harness --record predictions.json
    python -m algorithms.forecast_edge.harness --resolve
    python -m algorithms.forecast_edge.harness --score

Why forward and not back: a retrieval-enabled forecaster can look up what
happened, and a backtest cannot tell that apart from skill. Scored against
resolved markets an oracle returns a Brier skill of +0.999, which is exactly
what a spectacular strategy looks like. The only defence is to write the
prediction down while the answer does not yet exist.

Two integrity rules enforce that, and they are the whole point of the module:

  1. A prediction is refused once its market has closed. `--record` re-checks
     Gamma at record time rather than trusting the snapshot from `--ask`.
  2. A prediction is write-once. Re-recording a market that already has one
     is refused, so a forecast cannot be quietly revised toward the outcome.

The benchmark is never 0.5 and never the base rate. It is `market_price` at
the moment the question was asked — the number you would have to beat to make
money, captured in the same row. An earlier run of this comparison over 178
resolved markets scored the market at Brier 0.067 and a no-retrieval
forecaster at 0.156, worse than always guessing the base rate; this exists to
find out whether retrieval changes that.

Storage is its own SQLite file, like the archiver's: an offline research job
must never contend with live trading writes.
"""

import argparse
import json
import math
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from bot.polymarket import api

DEFAULT_DB = os.path.join("data", "forecast_log.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    market_id   TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    asked_at    INTEGER NOT NULL,
    question    TEXT NOT NULL,
    criteria    TEXT,
    end_date    TEXT,
    market_price REAL NOT NULL,      -- the benchmark, frozen at ask time
    fee_rate    REAL,
    volume      REAL,
    model_p     REAL,                -- NULL until --record
    model       TEXT,
    confidence  TEXT,
    rationale   TEXT,
    recorded_at INTEGER,
    outcome     REAL,                -- NULL until --resolve
    resolved_at INTEGER,
    PRIMARY KEY (market_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_fc_open ON forecasts(outcome, model_p);
"""


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _ts(iso) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# High-frequency series resolve in minutes and are coinflips on a price tick,
# not opinions about the world. They would swamp the sample.
_JUNK = ("up or down", "updown")


def _eligible(m: dict, now: float, min_days: float, max_days: float,
              min_volume: float, max_fee: float) -> bool:
    q = (m.get("question") or "").lower()
    if any(j in q for j in _JUNK) or "updown" in (m.get("slug") or "").lower():
        return False
    if m.get("sportsMarketType") or m.get("gameId"):
        return False            # sports is arbitraged elsewhere; measured dead
    fs = m.get("feeSchedule") or {}
    fee = float(fs.get("rate") or 0) if m.get("feesEnabled") else 0.0
    if fee > max_fee:
        return False
    if float(m.get("volumeNum") or m.get("volume") or 0) < min_volume:
        return False
    end = _ts(m.get("endDate"))
    if end is None:
        return False
    days = (end - now) / 86400.0
    return min_days <= days <= max_days


def _yes_price(m: dict) -> Optional[float]:
    """Mid of the YES leg. bestBid/bestAsk are the YES side on Gamma."""
    try:
        bid, ask = float(m.get("bestBid") or 0), float(m.get("bestAsk") or 0)
    except (TypeError, ValueError):
        return None
    if not (0 < bid < ask < 1):
        return None
    return round((bid + ask) / 2, 4)


def ask(conn, n: int, out_path: str, min_days: float, max_days: float,
        min_volume: float, max_fee: float) -> int:
    """Snapshot open markets and their prices; emit the questions file."""
    now = time.time()
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    seen, picked = set(), []
    for offset in range(0, 1500, 100):
        for m in api.fetch_top_markets(closed=False, limit=100, offset=offset):
            cid = m.get("conditionId")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if not _eligible(m, now, min_days, max_days, min_volume, max_fee):
                continue
            price = _yes_price(m)
            if price is None:
                continue
            picked.append((cid, m, price))
        if len(picked) >= n:
            break
    picked = picked[:n]

    rows, questions = [], []
    for cid, m, price in picked:
        fs = m.get("feeSchedule") or {}
        rows.append((
            cid, run_id, int(now), m.get("question") or "",
            (m.get("description") or "")[:900], (m.get("endDate") or "")[:19],
            price, float(fs.get("rate") or 0) if m.get("feesEnabled") else 0.0,
            float(m.get("volumeNum") or m.get("volume") or 0),
        ))
        questions.append({
            "market_id": cid,
            "question": m.get("question") or "",
            "resolves_on": (m.get("endDate") or "")[:10],
            "resolution_criteria": (m.get("description") or "")[:700],
        })
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO forecasts (market_id, run_id, asked_at, question,"
            " criteria, end_date, market_price, fee_rate, volume)"
            " VALUES (?,?,?,?,?,?,?,?,?)", rows)

    with open(out_path, "w") as fh:
        json.dump({"run_id": run_id, "questions": questions}, fh, indent=1)
    print(f"run {run_id}: {len(rows)} open markets snapshotted")
    print(f"  -> {out_path}   (send this to the forecaster; it contains no prices)")
    print(f"  then: --record <predictions.json>")
    return len(rows)


def record(conn, path: str, model: str) -> int:
    """Attach predictions. Refuses anything closed or already predicted."""
    payload = json.load(open(path))
    preds = payload.get("predictions", payload if isinstance(payload, list) else [])
    run_id = payload.get("run_id") if isinstance(payload, dict) else None

    written = skipped = 0
    for row in preds:
        mid = str(row.get("market_id") or row.get("id") or "")
        if not mid:
            continue
        where = "market_id = ?" + (" AND run_id = ?" if run_id else "")
        args = (mid, run_id) if run_id else (mid,)
        cur = conn.execute(
            f"SELECT market_id, run_id, model_p, end_date FROM forecasts WHERE {where}",
            args).fetchone()
        if cur is None:
            print(f"  ! unknown market {mid[:14]} — not asked in any run"); skipped += 1
            continue
        # Rule 2: write-once. A revised forecast is not a forecast.
        if cur["model_p"] is not None:
            print(f"  ! {mid[:14]} already has a prediction — refusing to overwrite")
            skipped += 1
            continue
        # Rule 1: the market must still be open *now*, not merely at ask time.
        m = api.fetch_market_resolution(mid)
        if m is not None and m.get("closed"):
            print(f"  ! {mid[:14]} has already closed — refusing (that is a backtest)")
            skipped += 1
            continue
        with conn:
            conn.execute(
                "UPDATE forecasts SET model_p = ?, model = ?, confidence = ?,"
                " rationale = ?, recorded_at = ? WHERE market_id = ? AND run_id = ?",
                (max(0.001, min(0.999, float(row["p"]))), model,
                 str(row.get("conf", "")), str(row.get("why", ""))[:500],
                 int(time.time()), cur["market_id"], cur["run_id"]))
        written += 1
    print(f"recorded {written} predictions, skipped {skipped}")
    return written


def resolve(conn) -> int:
    """Backfill outcomes for markets that have since settled."""
    rows = conn.execute(
        "SELECT market_id, run_id FROM forecasts WHERE outcome IS NULL"
        " AND model_p IS NOT NULL").fetchall()
    done = 0
    for r in rows:
        m = api.fetch_market_resolution(r["market_id"])
        if not m or not m.get("closed"):
            continue
        try:
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            names = [str(o).strip().lower() for o in json.loads(m.get("outcomes") or "[]")]
        except (ValueError, TypeError):
            continue
        if "yes" not in names or len(names) != len(prices) or max(prices) < 0.99:
            continue
        with conn:
            conn.execute(
                "UPDATE forecasts SET outcome = ?, resolved_at = ?"
                " WHERE market_id = ? AND run_id = ?",
                (1.0 if prices[names.index("yes")] >= 0.99 else 0.0,
                 int(time.time()), r["market_id"], r["run_id"]))
        done += 1
    print(f"resolved {done} of {len(rows)} outstanding")
    return done


def score(conn) -> None:
    rows = conn.execute(
        "SELECT * FROM forecasts WHERE outcome IS NOT NULL AND model_p IS NOT NULL"
    ).fetchall()
    if not rows:
        print("Nothing scored yet — predictions must resolve first.")
        return
    n = len(rows)
    base = sum(r["outcome"] for r in rows) / n
    bm = sum((r["model_p"] - r["outcome"]) ** 2 for r in rows) / n
    bk = sum((r["market_price"] - r["outcome"]) ** 2 for r in rows) / n
    bb = sum((base - r["outcome"]) ** 2 for r in rows) / n

    print(f"scored {n} resolved forecasts (YES base rate {base:.3f})\n")
    print("BRIER  (lower is better)")
    print(f"  {'market price':>22} {bk:.4f}")
    print(f"  {'forecaster':>22} {bm:.4f}")
    print(f"  {'always base rate':>22} {bb:.4f}")
    skill = 1 - bm / bk if bk else 0.0
    print(f"\n  skill vs market  {skill:+.3f}   (>0 = the forecaster beat the price)")

    extreme = [r for r in rows if r["model_p"] <= 0.05 or r["model_p"] >= 0.95]
    if extreme:
        right = sum(1 for r in extreme
                    if (r["model_p"] >= 0.95) == (r["outcome"] == 1.0))
        print(f"  extreme calls    {len(extreme)}/{n}, {right}/{len(extreme)} correct")
    if skill > 0.25:
        print("  ** skill this high on real forecasts is implausible — check that"
              " every prediction was recorded before its market closed **")

    print(f"\n{'edge':>8} {'bets':>6} {'ROI/trade':>11}   (half-spread 0.005, fee per market)")
    print("-" * 52)
    for thr in (0.05, 0.10, 0.20):
        pnl = k = 0
        for r in rows:
            p, mp, o = r["model_p"], r["market_price"], r["outcome"]
            side = 1 if p - mp > thr else (0 if mp - p > thr else None)
            if side is None:
                continue
            entry = min((mp if side else 1 - mp) + 0.005, 0.99)
            shares = 1.0 / entry
            fee = (r["fee_rate"] or 0) * entry * (1 - entry) * shares
            pnl += shares * (o if side else 1 - o) - 1.0 - fee
            k += 1
        print(f"{thr:>8.2f} {k:>6} {pnl/k if k else 0:>+10.1%}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=None)
    p.add_argument("--ask", type=int, metavar="N", help="snapshot N open markets")
    p.add_argument("--out", default="questions.json")
    p.add_argument("--record", metavar="FILE", help="attach predictions")
    p.add_argument("--model", default="claude", help="label for the forecaster")
    p.add_argument("--resolve", action="store_true")
    p.add_argument("--score", action="store_true")
    p.add_argument("--min-days", type=float, default=3.0)
    p.add_argument("--max-days", type=float, default=30.0)
    p.add_argument("--min-volume", type=float, default=10_000.0)
    p.add_argument("--max-fee", type=float, default=0.0,
                   help="0 keeps only fee-free markets, where crossing costs "
                        "just half a spread")
    args = p.parse_args()

    conn = connect(args.db)
    if args.ask:
        ask(conn, args.ask, args.out, args.min_days, args.max_days,
            args.min_volume, args.max_fee)
    if args.record:
        record(conn, args.record, args.model)
    if args.resolve:
        resolve(conn)
    if args.score:
        score(conn)
    if not any((args.ask, args.record, args.resolve, args.score)):
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
