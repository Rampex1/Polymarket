#!/usr/bin/env python3
"""
Prints a formatted account summary: balance, open positions, total value, P&L.
Shows paper and live sections separately.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config, db
from bot.positions import PositionTracker

tracker = PositionTracker()
conn = db.get()

W = 52
DIV = "─" * W

def pnl_str(v: float) -> str:
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

def print_section(title: str, paper: bool) -> None:
    p_flag = int(paper)

    positions  = tracker.all_open(paper=paper)
    exposure   = tracker.total_exposure_usdc(paper=paper)

    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_usdc), 0) FROM daily_stats"
    ).fetchone()
    today_pnl = float(row[0])  # daily_stats is shared; split by trade_log below

    # Per-mode P&L from trade_log
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log WHERE paper=?", (p_flag,)
    ).fetchone()
    total_pnl = float(row[0])

    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log WHERE paper=? AND date(ts,'unixepoch','localtime')=date('now','localtime')",
        (p_flag,)
    ).fetchone()
    today_pnl = float(row[0])

    row = conn.execute(
        "SELECT COUNT(*) FROM trade_log WHERE action='BUY' AND paper=?", (p_flag,)
    ).fetchone()
    total_buys = int(row[0])

    row = conn.execute(
        "SELECT COUNT(*) FROM trade_log WHERE action IN ('SELL','REDEEM') AND paper=?", (p_flag,)
    ).fetchone()
    total_closes = int(row[0])

    print()
    print("┌" + DIV + "┐")
    print(f"│{('  ' + title):^{W}}│")
    print("├" + DIV + "┤")

    if paper:
        balance = tracker.paper_balance()
        total_value = balance + exposure
        print(f"│  {'Available cash':<28} ${balance:>18,.2f}  │")
        print(f"│  {'Open position cost':<28} ${exposure:>18,.2f}  │")
        print("│  " + "·" * (W - 4) + "  │")
        print(f"│  {'Total value':<28} ${total_value:>18,.2f}  │")
    else:
        print(f"│  {'Open position cost':<28} ${exposure:>18,.2f}  │")
        print(f"│  {'(live balance tracked on Polymarket)':<{W-4}}  │")

    print("├" + DIV + "┤")
    print(f"│  {'Today P&L':<28} {pnl_str(today_pnl):>19}  │")
    print(f"│  {'All-time realized P&L':<28} {pnl_str(total_pnl):>19}  │")
    print(f"│  {'Total trades':<28} {f'{total_buys} buys / {total_closes} closes':>19}  │")
    print("├" + DIV + "┤")

    if positions:
        print(f"│{'  OPEN POSITIONS':^{W}}│")
        print("├" + DIV + "┤")
        for p in positions:
            label = f"{p.outcome[:20]}" if p.outcome else p.question[:20]
            est_value = p.shares * p.avg_price
            print(f"│  {label:<22} {p.shares:>7.2f} sh  ${est_value:>8.2f} cost  │")
        print("├" + DIV + "┤")

    if paper:
        print(f"│  {'Starting balance':<28} ${config.PAPER_STARTING_BALANCE:>18,.2f}  │")

    print("└" + DIV + "┘")


# Check which modes have activity
has_paper = conn.execute("SELECT COUNT(*) FROM trade_log WHERE paper=1").fetchone()[0] > 0
has_live  = conn.execute("SELECT COUNT(*) FROM trade_log WHERE paper=0").fetchone()[0] > 0
has_paper_positions = conn.execute("SELECT COUNT(*) FROM positions WHERE paper=1 AND shares>0").fetchone()[0] > 0
has_live_positions  = conn.execute("SELECT COUNT(*) FROM positions WHERE paper=0 AND shares>0").fetchone()[0] > 0

if has_paper or has_paper_positions:
    print_section("PAPER ACCOUNT", paper=True)

if has_live or has_live_positions:
    print_section("LIVE ACCOUNT", paper=False)

if not has_paper and not has_live and not has_paper_positions and not has_live_positions:
    print("\nNo trading activity yet.\n")
