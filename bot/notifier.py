"""
Discord notifications + midnight daily summary thread.

Messages are sent to a Discord channel via an incoming webhook. User-
controlled strings (market questions, outcomes, reasons) are escaped for
Discord markdown so a `*` or `_` in a title can't break formatting.

The midnight summary thread uses `config.TIMEZONE` instead of naive
`datetime.now()`. Without an explicit tz the rollover would happen at
whatever the VPS local time happened to be.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta

import requests as http

from . import config
from .models import Trade

logger = logging.getLogger(__name__)

# Discord hard-caps message content at 2000 chars.
_DISCORD_MAX_LEN = 2000


def send(text: str, webhook_url: str = "") -> None:
    """Fire-and-forget Discord webhook message. Silently skips if not configured.

    `webhook_url` overrides the global config URL — pass the algorithm's
    per-algo webhook so each algorithm posts to its own channel.
    """
    url = webhook_url or config.DISCORD_WEBHOOK_URL
    if not url:
        return
    try:
        http.post(url, json={"content": text[:_DISCORD_MAX_LEN]}, timeout=5)
    except Exception as e:
        logger.warning("Discord send failed: %s", e)


_DISCORD_ESCAPE = re.compile(r"([\\*_~`|>])")


def _esc(s: str) -> str:
    """Escape Discord markdown special chars in user-controlled strings."""
    return _DISCORD_ESCAPE.sub(r"\\\1", s or "")


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def _subtitle(algo_name: str) -> str:
    """Returns ' · algo_name' suffix for header lines, or '' if not set."""
    return f" · {_esc(algo_name)}" if algo_name else ""


def _q(question: str) -> str:
    """Bold the market question — the most important context in every message."""
    return f"**{_esc((question or '')[:80])}**"


def _feature_line(features: dict) -> str:
    """Compact human summary of the signal's logged features, for Discord.
    Only renders keys that are present — algorithms without features get ''."""
    if not features:
        return ""
    parts = []
    age = features.get("wallet_age_seconds")
    if age is not None:
        parts.append(f"wallet {age / 86_400:.1f}d old")
    trade_count = features.get("trade_count")
    if trade_count is not None:
        parts.append(f"{trade_count} prior trades")
    portfolio = features.get("portfolio_value_usdc")
    if portfolio is not None:
        parts.append(f"portfolio ${portfolio:,.0f}")
    category = features.get("market_category")
    if category:
        parts.append(_esc(str(category)))
    end_ts = features.get("market_end_ts")
    if end_ts is not None:
        days = max(0.0, (end_ts - time.time()) / 86_400)
        parts.append(f"resolves in {days:.0f}d")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Canned message helpers
# ---------------------------------------------------------------------------
#
# Each helper accepts an optional `algo_name` so the message can be
# disambiguated when multiple algorithms run side-by-side.


def on_signal(intent, algo_name: str = "", webhook_url: str = "") -> None:
    """Generic detection notification — intent-based, works for any algorithm."""
    from .algorithm import OpenIntent, CloseIntent, SettleIntent
    if isinstance(intent, OpenIntent):
        action_line = (
            f"OPEN `{_esc(intent.outcome) or '—'}`  "
            f"${intent.usdc_amount:,.2f} @ **{intent.signal_price:.3f}**"
        )
    elif isinstance(intent, CloseIntent):
        action_line = (
            f"CLOSE `{_esc(intent.outcome) or 'position'}`  "
            f"{intent.fraction * 100:.0f}% @ **{intent.signal_price:.3f}**"
        )
    elif isinstance(intent, SettleIntent):
        action_line = f"SETTLE `{_esc(intent.outcome) or 'position'}`"
    else:
        action_line = "Unknown intent"

    lines = [
        f"📥 **SIGNAL**{_subtitle(algo_name)}",
        _q(intent.question),
        action_line,
    ]
    reason = getattr(intent, "reason", "")
    if reason:
        lines.append(f"↳ {_esc(reason)}")
    feature_summary = _feature_line(getattr(intent, "features", None) or {})
    if feature_summary:
        lines.append(f"↳ {feature_summary}")
    send("\n".join(lines), webhook_url=webhook_url)


def on_trade_detected(trade: Trade, algo_name: str = "", webhook_url: str = "") -> None:
    """Legacy entry point — kept for the wallet-watching CopyTrade flow that
    classifies a Trade before issuing an Intent. New algorithms should call
    `on_signal` instead."""
    send(
        f"📥 **SIGNAL**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(trade.action)} `{_esc(trade.outcome)}`  "
        f"${trade.size_usdc:,.0f} @ **{trade.price:.3f}**",
        webhook_url=webhook_url,
    )


def on_buy_executed(
    trade: Trade, spent_usdc: float, paper: bool, fill_price: float,
    algo_name: str = "", webhook_url: str = "",
) -> None:
    tag = "📄 **PAPER BUY**" if paper else "✅ **BUY**"
    shares = spent_usdc / fill_price if fill_price > 0 else 0.0
    slip = ""
    if trade.price > 0 and fill_price > 0:
        drift_pct = (fill_price - trade.price) / trade.price * 100
        slip = f" _({drift_pct:+.1f}% slip)_"
    send(
        f"{tag}{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"`{_esc(trade.outcome)}`  ${spent_usdc:.2f} → {shares:.2f} shares "
        f"@ **{fill_price:.3f}**{slip}",
        webhook_url=webhook_url,
    )


def on_buy_failed(trade: Trade, reason: str, algo_name: str = "", webhook_url: str = "") -> None:
    send(
        f"❌ **BUY failed**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(reason)}",
        webhook_url=webhook_url,
    )


def on_sell_executed(
    trade: Trade, shares: float, pnl: float, paper: bool, fill_price: float,
    algo_name: str = "", webhook_url: str = "",
) -> None:
    tag = "📄 **PAPER SELL**" if paper else "✅ **SELL**"
    sign = "+" if pnl >= 0 else ""
    send(
        f"{tag}{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"`{_esc(trade.outcome)}`  {shares:.2f} shares @ **{fill_price:.3f}** · "
        f"P&L **{sign}${pnl:.2f}**",
        webhook_url=webhook_url,
    )


def on_risk_blocked(reason: str, trade: Trade, algo_name: str = "", webhook_url: str = "") -> None:
    send(
        f"⚠️ **Risk block**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(reason)}",
        webhook_url=webhook_url,
    )


def on_startup(mode: str, exposure: float, algo_name: str = "", webhook_url: str = "") -> None:
    send(
        f"🚀 **Bot started**{_subtitle(algo_name)} — {_esc(mode)} mode\n"
        f"Exposure: **${exposure:.2f}**",
        webhook_url=webhook_url,
    )


def on_shutdown(algo_name: str = "", webhook_url: str = "") -> None:
    send(f"🛑 **Bot stopped**{_subtitle(algo_name)}", webhook_url=webhook_url)


# ---------------------------------------------------------------------------
# Daily summary background thread
# ---------------------------------------------------------------------------

def start_daily_summary(
    tracker,
    paper: bool,
    stop_event: threading.Event | None = None,
    algo_name: str = "",
    webhook_url: str = "",
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
            _send_daily_summary(tracker, paper=paper, algo_name=algo_name,
                                webhook_url=webhook_url)

    thread_name = f"daily-summary-{algo_name}" if algo_name else "daily-summary"
    t = threading.Thread(target=_loop, daemon=True, name=thread_name)
    t.start()
    logger.info(
        "%sDaily summary thread started (timezone=%s, mode=%s).",
        _subtitle(algo_name) or "", config.TIMEZONE.key, "PAPER" if paper else "LIVE",
    )


def _send_daily_summary(tracker, paper: bool, algo_name: str = "", webhook_url: str = "") -> None:
    positions = tracker.all_open(paper=paper)
    exposure = tracker.total_exposure_usdc(paper=paper)
    pnl = tracker.today_pnl_usdc(paper=paper)
    sign = "+" if pnl >= 0 else ""
    today_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d")
    mode_tag = "PAPER" if paper else "LIVE"

    lines = [
        f"📊 **Daily Summary · {today_str} · {mode_tag}**{_subtitle(algo_name)}",
        f"> Realized P&L: **{sign}${pnl:.2f}**",
        f"> Open: {len(positions)} positions · Exposure: **${exposure:.2f}**",
    ]
    if positions:
        lines.append(">")
        for p in positions:
            lines.append(
                f"> `{_esc(p.outcome)}` {p.shares:.1f}sh @ {p.avg_price:.3f} · "
                f"{_esc(p.question[:50])}"
            )
    send("\n".join(lines), webhook_url=webhook_url)


def send_profile_summary(
    algo_infos: list,
    webhook_url: str,
    profile: str = "",
) -> None:
    """Profile-level daily summary — one message per profile to a shared channel.

    `algo_infos` is a list of (algo_name, paper) tuples. Fresh PositionTracker
    instances are created per algo so this can run from any thread without
    holding live tracker references.

    Layout:
      📊 **Daily Summary · DATE · PROFILE**

      **algo_name** · PAPER/LIVE
      > Realized P&L: +$X.XX
      > Open: N positions · Exposure: $X.XX
      > `OUTCOME` Xsh @ X.XXX · Question text…
      (blank line between algos)
      ────────────────────
      **Total · N algorithms**
      > P&L: +$X.XX · Exposure: $X.XX
    """
    if not webhook_url:
        return

    from bot.positions import PositionTracker

    today_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d")
    profile_label = _esc(profile) if profile else "all"

    lines = [f"📊 **Daily Summary · {today_str} · {profile_label}**", ""]

    total_pnl = 0.0
    total_exposure = 0.0

    for name, paper in algo_infos:
        tracker = PositionTracker(algo=name)
        positions = tracker.all_open(paper=paper)
        exposure = tracker.total_exposure_usdc(paper=paper)
        pnl = tracker.today_pnl_usdc(paper=paper)
        total_pnl += pnl
        total_exposure += exposure

        sign = "+" if pnl >= 0 else ""
        mode_tag = "PAPER" if paper else "LIVE"
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        lines.append(f"**{_esc(name)}** · {mode_tag}")
        lines.append(f"> {pnl_emoji} Realized P&L: **{sign}${pnl:.2f}**")
        lines.append(f"> Open: {len(positions)} positions · Exposure: **${exposure:.2f}**")
        if positions:
            lines.append(">")
            for p in positions:
                lines.append(
                    f"> `{_esc(p.outcome)}` {p.shares:.1f}sh @ {p.avg_price:.3f}"
                    f"  ·  {_esc(p.question[:55])}"
                )
        lines.append("")

    sign = "+" if total_pnl >= 0 else ""
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    algo_word = "algorithm" if len(algo_infos) == 1 else "algorithms"
    lines.append("─" * 22)
    lines.append(f"**Total · {len(algo_infos)} {algo_word}**")
    lines.append(f"> {pnl_emoji} P&L: **{sign}${total_pnl:.2f}** · Exposure: **${total_exposure:.2f}**")

    send("\n".join(lines), webhook_url=webhook_url)


def _seconds_until_midnight() -> float:
    """Time until the next midnight in the configured timezone."""
    now = datetime.now(tz=config.TIMEZONE)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1.0, (midnight - now).total_seconds())
