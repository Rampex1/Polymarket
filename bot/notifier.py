"""
Discord notifications + midnight daily summary thread.

Messages are sent to a Discord channel via an incoming webhook. User-
controlled strings (market titles, outcomes, error reasons) are escaped
for Discord markdown so a `*` or `_` in a title can't break formatting.

The midnight summary thread uses `config.TIMEZONE` instead of naive
`datetime.now()`. Without an explicit tz the rollover would happen at
whatever the VPS local time happened to be.
"""

import logging
import re
import threading
from datetime import datetime, timedelta

import requests as http

from . import config
from .models import Trade

logger = logging.getLogger(__name__)

# Discord hard-caps message content at 2000 chars.
_DISCORD_MAX_LEN = 2000


def send(text: str) -> None:
    """Fire-and-forget Discord webhook message. Silently skips if not configured."""
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        http.post(
            config.DISCORD_WEBHOOK_URL,
            json={"content": text[:_DISCORD_MAX_LEN]},
            timeout=5,
        )
    except Exception as e:
        logger.warning("Discord send failed: %s", e)


_DISCORD_ESCAPE = re.compile(r"([\\*_~`|>])")


def _esc(s: str) -> str:
    """Escape Discord markdown special chars in user-controlled strings."""
    return _DISCORD_ESCAPE.sub(r"\\\1", s or "")


# ---------------------------------------------------------------------------
# Canned message helpers
# ---------------------------------------------------------------------------
#
# Each helper accepts an optional `algo_name` so the message can be
# disambiguated when multiple algorithms run side-by-side. Pass "" to
# suppress the prefix (single-algo deployments).


def _prefix(algo_name: str) -> str:
    return f"[{_esc(algo_name)}] " if algo_name else ""


def on_signal(intent, algo_name: str = "") -> None:
    """Generic detection notification — intent-based, works for any algorithm."""
    from .algorithm import OpenIntent, CloseIntent, SettleIntent
    if isinstance(intent, OpenIntent):
        line = (
            f"OPEN {_esc(intent.outcome) or '—'} — "
            f"${intent.usdc_amount:,.2f} @ {intent.signal_price:.3f}"
        )
    elif isinstance(intent, CloseIntent):
        line = (
            f"CLOSE {intent.fraction * 100:.0f}% of {_esc(intent.outcome) or 'position'} "
            f"@ {intent.signal_price:.3f}"
        )
    elif isinstance(intent, SettleIntent):
        line = f"SETTLE {_esc(intent.outcome) or 'position'}"
    else:
        line = "Unknown intent"

    send(
        f"{_prefix(algo_name)}👀 **Signal detected**\n"
        f"{line}\n"
        f"{_esc((intent.question or '')[:80])}"
    )


def on_trade_detected(trade: Trade, algo_name: str = "") -> None:
    """Legacy entry point — kept for the wallet-watching CopyTrade flow that
    classifies a Trade before issuing an Intent. New algorithms should call
    `on_signal` instead."""
    send(
        f"{_prefix(algo_name)}👀 **Signal detected**\n"
        f"{_esc(trade.action)} {_esc(trade.outcome)} — "
        f"${trade.size_usdc:,.0f} @ {trade.price:.3f}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_buy_executed(
    trade: Trade, spent_usdc: float, paper: bool, fill_price: float,
    algo_name: str = "",
) -> None:
    tag = "📄 PAPER" if paper else "✅ BUY"
    send(
        f"{_prefix(algo_name)}{tag}\n"
        f"${spent_usdc:.2f} of {_esc(trade.outcome)} @ {fill_price:.3f}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_buy_failed(trade: Trade, reason: str, algo_name: str = "") -> None:
    send(
        f"{_prefix(algo_name)}❌ **BUY failed**\n"
        f"{_esc(reason)}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_sell_executed(
    trade: Trade, shares: float, pnl: float, paper: bool, fill_price: float,
    algo_name: str = "",
) -> None:
    tag = "📄 PAPER" if paper else "✅ SELL"
    sign = "+" if pnl >= 0 else ""
    send(
        f"{_prefix(algo_name)}{tag}\n"
        f"{shares:.2f} shares of {_esc(trade.outcome)} @ {fill_price:.3f} "
        f"| P&L {sign}${pnl:.2f}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_risk_blocked(reason: str, trade: Trade, algo_name: str = "") -> None:
    send(
        f"{_prefix(algo_name)}⚠️ **Risk block**\n"
        f"{_esc(reason)}\n"
        f"{_esc(trade.question[:80])}"
    )


def on_slippage_skipped(trade: Trade, drift_pct: float, algo_name: str = "") -> None:
    send(
        f"{_prefix(algo_name)}⏭ **Slippage skip** ({drift_pct:.1f}%)\n"
        f"{_esc(trade.question[:80])}"
    )


def on_startup(mode: str, exposure: float, algo_name: str = "") -> None:
    send(
        f"{_prefix(algo_name)}🚀 **Bot started** — {_esc(mode)} mode\n"
        f"Exposure: ${exposure:.2f}"
    )


def on_shutdown(algo_name: str = "") -> None:
    send(f"{_prefix(algo_name)}🛑 **Bot stopped**")


# ---------------------------------------------------------------------------
# Daily summary background thread
# ---------------------------------------------------------------------------

def start_daily_summary(
    tracker,
    paper: bool,
    stop_event: threading.Event | None = None,
    algo_name: str = "",
) -> None:
    """Send a portfolio summary every day at midnight (config.TIMEZONE).

    Per-algorithm: each worker thread starts its own daily-summary thread
    against its own tracker. The `algo_name` tag disambiguates messages
    when multiple algorithms are active.
    """
    def _loop() -> None:
        while True:
            wait_s = _seconds_until_midnight()
            if stop_event is not None:
                if stop_event.wait(wait_s):
                    return
            else:
                threading.Event().wait(wait_s)
            _send_daily_summary(tracker, paper=paper, algo_name=algo_name)

    thread_name = f"daily-summary-{algo_name}" if algo_name else "daily-summary"
    t = threading.Thread(target=_loop, daemon=True, name=thread_name)
    t.start()
    logger.info(
        "%sDaily summary thread started (timezone=%s, mode=%s).",
        _prefix(algo_name), config.TIMEZONE.key, "PAPER" if paper else "LIVE",
    )


def _send_daily_summary(tracker, paper: bool, algo_name: str = "") -> None:
    positions = tracker.all_open(paper=paper)
    exposure = tracker.total_exposure_usdc(paper=paper)
    pnl = tracker.today_pnl_usdc(paper=paper)
    sign = "+" if pnl >= 0 else ""
    today_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d")
    mode_tag = "📄 PAPER" if paper else "💵 LIVE"

    lines = [
        f"{_prefix(algo_name)}📊 **Daily Summary — {today_str}** ({mode_tag})",
        f"Realized P&L: {sign}${pnl:.2f}",
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
