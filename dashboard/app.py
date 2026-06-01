import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template
from bot import config, db
from bot.positions import PositionTracker

app = Flask(__name__)
tracker = PositionTracker()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    conn = db.get()

    balance  = tracker.paper_balance()
    exposure = tracker.total_exposure_usdc()
    today_pnl = tracker.today_pnl_usdc()

    row = conn.execute("SELECT COALESCE(SUM(realized_pnl_usdc),0) FROM daily_stats").fetchone()
    total_pnl = float(row[0])

    row = conn.execute("SELECT COUNT(*) FROM trade_log WHERE action='BUY'").fetchone()
    total_buys = int(row[0])
    row = conn.execute("SELECT COUNT(*) FROM trade_log WHERE action IN ('SELL','REDEEM')").fetchone()
    total_closes = int(row[0])

    positions = [
        {
            "outcome":    p.outcome or "—",
            "question":   p.question,
            "shares":     round(p.shares, 4),
            "avg_price":  round(p.avg_price, 4),
            "cost":       round(p.total_cost_usdc, 2),
        }
        for p in tracker.all_open()
    ]

    rows = conn.execute(
        """SELECT action, outcome, question, shares, price, usdc_amount, realized_pnl,
                  datetime(ts,'unixepoch','localtime') as time
           FROM trade_log ORDER BY ts DESC LIMIT 50"""
    ).fetchall()
    trades = [
        {
            "action":   r["action"],
            "outcome":  r["outcome"] or "—",
            "question": r["question"] or "—",
            "shares":   round(r["shares"] or 0, 4),
            "price":    round(r["price"] or 0, 4),
            "amount":   round(r["usdc_amount"] or 0, 2),
            "pnl":      round(r["realized_pnl"] or 0, 2),
            "time":     r["time"],
        }
        for r in rows
    ]

    daily = conn.execute(
        "SELECT date, realized_pnl_usdc FROM daily_stats ORDER BY date"
    ).fetchall()
    cumulative, running = [], 0.0
    for d in daily:
        running += d["realized_pnl_usdc"]
        cumulative.append({"date": d["date"], "pnl": round(running, 2)})

    return jsonify({
        "balance":       round(balance, 2),
        "exposure":      round(exposure, 2),
        "total_value":   round(balance + exposure, 2),
        "today_pnl":     round(today_pnl, 2),
        "total_pnl":     round(total_pnl, 2),
        "total_buys":    total_buys,
        "total_closes":  total_closes,
        "starting":      config.PAPER_STARTING_BALANCE,
        "positions":     positions,
        "trades":        trades,
        "daily_pnl":     cumulative,
    })


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5001)
