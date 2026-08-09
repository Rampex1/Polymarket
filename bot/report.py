"""
Performance report CLI — the "check analytics" half of the config loop.

    python -m bot.report

Reports on every algorithm that has left tracks in the DB (not just the
ones in the current profile), one section per (algo, paper/live) pool:
bankroll, exposure, realized P&L, signal counts, skip reasons, and the
thesis-critical number — win rate vs. average entry odds on labeled
signals. Read-only.
"""

import time

from .storage import db


def _one(conn, sql: str, args=()) -> float:
    row = conn.execute(sql, args).fetchone()
    return row[0] if row and row[0] is not None else 0


def _all_algos(conn) -> list[str]:
    rows = conn.execute("""
        SELECT algo FROM positions UNION SELECT algo FROM trade_log
        UNION SELECT algo FROM paper_account UNION SELECT algo FROM signals
        UNION SELECT algo FROM runs
    """).fetchall()
    return sorted(r[0] for r in rows)


def _pnl(v: float) -> str:
    return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def _section(conn, algo: str, paper: int) -> None:
    label = "PAPER" if paper else "LIVE"
    print(f"\n── {algo} [{label}] " + "─" * max(0, 46 - len(algo) - len(label)))

    last_run = conn.execute(
        "SELECT mode, profile, git_sha, started_at FROM runs "
        "WHERE algo=? ORDER BY started_at DESC LIMIT 1", (algo,),
    ).fetchone()
    if last_run:
        age_h = (time.time() - last_run["started_at"]) / 3600
        sha = last_run["git_sha"] or "?"
        print(f"  last boot: {age_h:,.1f}h ago  profile={last_run['profile']}  git={sha}")

    if paper:
        bal = _one(conn, "SELECT balance FROM paper_account WHERE algo=?", (algo,))
        print(f"  bankroll:  ${bal:,.2f}")

    n_open = _one(conn,
        "SELECT COUNT(*) FROM positions WHERE algo=? AND paper=? AND shares>0",
        (algo, paper))
    exposure = _one(conn,
        "SELECT COALESCE(SUM(total_cost_usdc),0) FROM positions "
        "WHERE algo=? AND paper=? AND shares>0", (algo, paper))
    print(f"  open:      {int(n_open)} positions, ${exposure:,.2f} at cost")

    pnl_all = _one(conn,
        "SELECT COALESCE(SUM(realized_pnl),0) FROM trade_log WHERE algo=? AND paper=?",
        (algo, paper))
    pnl_today = _one(conn,
        "SELECT COALESCE(SUM(realized_pnl),0) FROM trade_log WHERE algo=? AND paper=? "
        "AND date(ts,'unixepoch','localtime')=date('now','localtime')", (algo, paper))
    n_trades = _one(conn,
        "SELECT COUNT(*) FROM trade_log WHERE algo=? AND paper=?", (algo, paper))
    print(f"  realized:  {_pnl(pnl_all)} all-time, {_pnl(pnl_today)} today "
          f"({int(n_trades)} trades)")

    n_sig = _one(conn, "SELECT COUNT(*) FROM signals WHERE algo=? AND paper=?",
                 (algo, paper))
    if n_sig:
        n_exec = _one(conn,
            "SELECT COUNT(*) FROM signals WHERE algo=? AND paper=? AND executed=1",
            (algo, paper))
        print(f"  signals:   {int(n_sig)} total, {int(n_exec)} executed")

        skips = conn.execute(
            "SELECT skip_reason, COUNT(*) c FROM signals "
            "WHERE algo=? AND paper=? AND executed=0 AND skip_reason IS NOT NULL "
            "GROUP BY skip_reason ORDER BY c DESC LIMIT 3", (algo, paper),
        ).fetchall()
        if skips:
            top = ", ".join(f"{r['skip_reason']}×{r['c']}" for r in skips)
            print(f"  top skips: {top}")

        labeled = conn.execute(
            "SELECT COUNT(*) n, AVG(outcome >= 0.5) wins, AVG(signal_price) odds, "
            "COALESCE(SUM(pnl_usdc),0) pnl FROM signals "
            "WHERE algo=? AND paper=? AND outcome IS NOT NULL", (algo, paper),
        ).fetchone()
        if labeled and labeled["n"]:
            print(f"  resolved:  {labeled['n']} signals — win rate "
                  f"{labeled['wins'] * 100:.0f}% vs avg entry odds "
                  f"{labeled['odds'] * 100:.0f}% → {_pnl(labeled['pnl'])}")


def main() -> None:
    conn = db.get()
    algos = _all_algos(conn)
    if not algos:
        print("No activity recorded yet.")
        return
    for algo in algos:
        for paper in (1, 0):
            has_rows = _one(conn,
                "SELECT EXISTS(SELECT 1 FROM trade_log WHERE algo=? AND paper=? "
                "UNION SELECT 1 FROM positions WHERE algo=? AND paper=? AND shares>0 "
                "UNION SELECT 1 FROM signals WHERE algo=? AND paper=?)",
                (algo, paper, algo, paper, algo, paper))
            if has_rows or (paper and _one(conn,
                    "SELECT EXISTS(SELECT 1 FROM paper_account WHERE algo=?)", (algo,))):
                _section(conn, algo, paper)
    print()


if __name__ == "__main__":
    main()
