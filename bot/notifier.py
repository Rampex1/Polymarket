"""
Telegram notifications + midnight daily summary thread.

Two fixes vs. the previous version:

  * All user-injected strings (market titles, outcomes) are HTML-escaped
    before being sent with `parse_mode=HTML`. A title containing `<`, `>`,
    or `&` was previously rejected by Telegram and the exception swallowed
    — meaning critical buy/sell alerts could silently disappear.

  * The midnight summary thread uses `config.TIMEZONE` instead of naive
    `datetime.now()`. Without an explicit tz the rollover happened at
    whatever the VPS local time happened to be.
"""

import html
import logging
import threading
from datetime import datetime, timedelta

import requests as http

from . import config
from .models import Trade

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


def _esc(s: str) -> str:
    """HTML-escape user-controlled strings before interpolating into a message."""
    return html.escape(s or "", quote=False)


# ---------------------------------------------------------------------------
# Canned message helpers
# ---------------------------------------------------------------------------

def on_trade_detected(trade: Trade) -> None:
    send(
        f"👀 <b>Signal detected</b>\n"
        f"{_esc(trade.action)} {_esc(trade.outcome)} — "
        f"${trade.size_usdc:,.0f} @ {trade.price:.3f}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_buy_executed(trade: Trade, spent_usdc: float, paper: bool, fill_price: float) -> None:
    tag = "📄 PAPER" if paper else "✅ BUY"
    send(
        f"{tag}\n"
        f"${spent_usdc:.2f} of {_esc(trade.outcome)} @ {fill_price:.3f}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_buy_failed(trade: Trade, reason: str) -> None:
    send(
        f"❌ <b>BUY failed</b>\n"
        f"{_esc(reason)}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_sell_executed(
    trade: Trade, shares: float, pnl: float, paper: bool, fill_price: float
) -> None:
    tag = "📄 PAPER" if paper else "✅ SELL"
    sign = "+" if pnl >= 0 else ""
    send(
        f"{tag}\n"
        f"{shares:.2f} shares of {_esc(trade.outcome)} @ {fill_price:.3f} "
        f"| P&amp;L {sign}${pnl:.2f}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_risk_blocked(reason: str, trade: Trade) -> None:
    send(
        f"⚠️ <b>Risk block</b>\n"
        f"{_esc(reason)}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_slippage_skipped(trade: Trade, drift_pct: float) -> None:
    send(
        f"⏭ <b>Slippage skip</b> ({drift_pct:.1f}%)\n"
        f"{_esc(trade.question[:80])}"
    )


def on_startup(mode: str, exposure: float) -> None:
    send(
        f"🚀 <b>Bot started</b> — {_esc(mode)} mode\n"
        f"Exposure: ${exposure:.2f}"
    )


def on_shutdown() -> None:
    send("🛑 <b>Bot stopped</b>")


# ---------------------------------------------------------------------------
# Daily summary background thread
# ---------------------------------------------------------------------------

def start_daily_summary(tracker, stop_event: threading.Event | None = None) -> None:
    """Send a portfolio summary every day at midnight (config.TIMEZONE)."""
    def _loop() -> None:
        while True:
            wait_s = _seconds_until_midnight()
            # If we have a stop_event, wake on shutdown signal instead of
            # blocking the daemon for hours past the real exit.
            if stop_event is not None:
                if stop_event.wait(wait_s):
                    return
            else:
                # Use Event-less sleep as a fallback for callers that
                # don't pass one. Daemon thread dies with the process.
                threading.Event().wait(wait_s)
            _send_daily_summary(tracker)

    t = threading.Thread(target=_loop, daemon=True, name="daily-summary")
    t.start()
    logger.info("Daily summary thread started (timezone=%s).", config.TIMEZONE.key)


def _send_daily_summary(tracker) -> None:
    positions = tracker.all_open()
    exposure = tracker.total_exposure_usdc()
    pnl = tracker.today_pnl_usdc()
    sign = "+" if pnl >= 0 else ""
    today_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d")

    lines = [
        f"📊 <b>Daily Summary — {today_str}</b>",
        f"Realized P&amp;L: {sign}${pnl:.2f}",
        f"Open positions: {len(positions)} | Exposure: ${exposure:.2f}",
    ]
    for p in positions:
        lines.append(
            f"  • {_esc(p.outcome)} {p.shares:.1f}sh @ {p.avg_price:.3f} — "
            f"{_esc(p.question[:45])}"
        )

    send("\n".join(lines))


def _seconds_until_midnight() -> float:
    """Time until the next midnight in the configured timezone."""
    now = datetime.now(tz=config.TIMEZONE)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1.0, (midnight - now).total_seconds())
