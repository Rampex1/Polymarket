#!/usr/bin/env python3
"""
Prints a per-algorithm account summary: balance, open positions, P&L.
Breaks out paper and live sections separately for each algorithm.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algorithms import ENABLED
from bot import db
from bot.ledger import Ledger

conn = db.get()

W = 52
DIV = "─" * W


def pnl_str(v: float) -> str:
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def print_section(algo, title: str, paper: bool) -> None:
    name = algo.params.name
    p_flag = int(paper)
    ledger = Ledger(algo=name)

    positions = ledger.all_open(paper=paper)
    exposure = ledger.total_exposure_usdc(paper=paper)

    total_pnl = float(conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log "
        "WHERE paper=? AND algo=?",
        (p_flag, name),
    ).fetchone()[0])

    today_pnl = float(conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_log "
        "WHERE paper=? AND algo=? AND date(ts,'unixepoch','localtime')=date('now','localtime')",
        (p_flag, name),
    ).fetchone()[0])

    total_buys = int(conn.execute(
        "SELECT COUNT(*) FROM trade_log WHERE action='BUY' AND paper=? AND algo=?",
        (p_flag, name),
    ).fetchone()[0])

    total_closes = int(conn.execute(
        "SELECT COUNT(*) FROM trade_log "
        "WHERE action IN ('SELL','REDEEM') AND paper=? AND algo=?",
        (p_flag, name),
    ).fetchone()[0])

    print()
    print("┌" + DIV + "┐")
    print(f"│{('  ' + title):^{W}}│")
    print("├" + DIV + "┤")

    if paper:
        balance = ledger.paper_balance()
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
        print(f"│  {'Starting balance':<28} ${algo.params.paper_starting_balance:>18,.2f}  │")

    print("└" + DIV + "┘")


printed_anything = False
for algo in ENABLED:
    name = algo.params.name
    has_paper = conn.execute(
        "SELECT COUNT(*) FROM trade_log WHERE paper=1 AND algo=?", (name,),
    ).fetchone()[0] > 0
    has_live = conn.execute(
        "SELECT COUNT(*) FROM trade_log WHERE paper=0 AND algo=?", (name,),
    ).fetchone()[0] > 0
    has_paper_positions = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE paper=1 AND shares>0 AND algo=?",
        (name,),
    ).fetchone()[0] > 0
    has_live_positions = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE paper=0 AND shares>0 AND algo=?",
        (name,),
    ).fetchone()[0] > 0

    title_upper = name.upper().replace("_", " ")

    if has_paper or has_paper_positions:
        print_section(algo, f"{title_upper} — PAPER", paper=True)
        printed_anything = True
    if has_live or has_live_positions:
        print_section(algo, f"{title_upper} — LIVE", paper=False)
        printed_anything = True

if not printed_anything:
    print("\nNo trading activity yet.\n")
