"""
Phase 5: Telegram notifications + daily portfolio summary thread.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

import requests as http

from . import config

logger = logging.getLogger(__name__)

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str) -> None:
    """Fire-and-forget Telegram message. Silently skips if creds not configured."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        http.post(
            _TELEGRAM_URL.format(token=config.TELEGRAM_BOT_TOKEN),
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=5,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Canned message helpers
# ---------------------------------------------------------------------------

def on_trade_detected(trade) -> None:
    send(
        f"👀 <b>Signal detected</b>\n"
        f"{trade.action} {trade.outcome} — ${trade.size_usdc:,.0f} @ {trade.price:.3f}\n"
        f"{trade.question[:80]}"
    )


def on_buy_executed(trade, scaled_usdc: float, paper: bool) -> None:
    tag = "📄 PAPER" if paper else "✅ BUY"
    send(
        f"{tag}\n"
        f"${scaled_usdc:.2f} of {trade.outcome} @ {trade.price:.3f}\n"
        f"{trade.question[:80]}"
    )


def on_sell_executed(trade, shares: float, pnl: float, paper: bool) -> None:
    tag = "📄 PAPER" if paper else "✅ SELL"
    sign = "+" if pnl >= 0 else ""
    send(
        f"{tag}\n"
        f"{shares:.2f} shares of {trade.outcome} @ {trade.price:.3f} | P&L {sign}${pnl:.2f}\n"
        f"{trade.question[:80]}"
    )


def on_risk_blocked(reason: str, trade) -> None:
    send(
        f"⚠️ <b>Risk block</b>\n"
        f"{reason}\n"
        f"{trade.question[:80]}"
    )


def on_slippage_skipped(trade, drift_pct: float) -> None:
    send(
        f"⏭ <b>Slippage skip</b> ({drift_pct:.1f}%)\n"
        f"{trade.question[:80]}"
    )


def on_startup(mode: str, exposure: float) -> None:
    send(
        f"🚀 <b>Bot started</b> — {mode} mode\n"
        f"Exposure: ${exposure:.2f}"
    )


def on_shutdown() -> None:
    send("🛑 <b>Bot stopped</b>")


# ---------------------------------------------------------------------------
# Daily summary background thread
# ---------------------------------------------------------------------------

def start_daily_summary(tracker) -> None:
    """Sends a portfolio summary every day at midnight. Runs as a daemon thread."""
    def _loop() -> None:
        while True:
            time.sleep(_seconds_until_midnight())
            _send_daily_summary(tracker)

    t = threading.Thread(target=_loop, daemon=True, name="daily-summary")
    t.start()
    logger.info("Daily summary thread started.")


def _send_daily_summary(tracker) -> None:
    positions = tracker.all_open()
    exposure = tracker.total_exposure_usdc()
    pnl = tracker.today_pnl_usdc()
    sign = "+" if pnl >= 0 else ""

    lines = [
        f"📊 <b>Daily Summary — {datetime.now().strftime('%Y-%m-%d')}</b>",
        f"Realized P&L: {sign}${pnl:.2f}",
        f"Open positions: {len(positions)} | Exposure: ${exposure:.2f}",
    ]
    for p in positions:
        lines.append(f"  • {p.outcome} {p.shares:.1f}sh @ {p.avg_price:.3f} — {p.question[:45]}")

    send("\n".join(lines))


def _seconds_until_midnight() -> float:
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (midnight - now).total_seconds()
