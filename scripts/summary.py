"""
Prints a formatted account summary: balance, open positions, total value, P&L.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config, db
from bot.positions import PositionTracker

tracker = PositionTracker()

balance    = tracker.paper_balance()
positions  = tracker.all_open()
exposure   = tracker.total_exposure_usdc()
today_pnl  = tracker.today_pnl_usdc()

# All-time realized P&L from daily_stats
conn = db.get()
row = conn.execute("SELECT COALESCE(SUM(realized_pnl_usdc), 0) FROM daily_stats").fetchone()
total_pnl = float(row[0])

# Trade counts
row = conn.execute("SELECT COUNT(*) FROM trade_log WHERE action='BUY'").fetchone()
total_buys = int(row[0])
row = conn.execute("SELECT COUNT(*) FROM trade_log WHERE action='SELL' OR action='REDEEM'").fetchone()
total_closes = int(row[0])

total_value = balance + exposure

W = 52
DIV = "─" * W

def pnl_str(v: float) -> str:
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

print()
print("┌" + DIV + "┐")
print(f"│{'  PAPER ACCOUNT SUMMARY':^{W}}│")
print("├" + DIV + "┤")
print(f"│  {'Available cash':<28} ${balance:>18,.2f}  │")
print(f"│  {'Open position cost':<28} ${exposure:>18,.2f}  │")
print("│  " + "·" * (W - 4) + "  │")
print(f"│  {'Total value':<28} ${total_value:>18,.2f}  │")
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
    print(f"│  {'Starting balance':<28} ${config.PAPER_STARTING_BALANCE:>18,.2f}  │")
else:
    print(f"│{'  No open positions':^{W}}│")
    print("├" + DIV + "┤")
    print(f"│  {'Starting balance':<28} ${config.PAPER_STARTING_BALANCE:>18,.2f}  │")

print("└" + DIV + "┘")
print()
