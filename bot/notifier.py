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
import threading
import time
from datetime import datetime, timedelta

import requests as http

from . import config, threads
from .models import Trade
from .notifications.messages import (
    escape as _esc,
    feature_line as _feature_line,
    market_url as _market_url,
    question as _q,
    subtitle as _subtitle,
)

logger = logging.getLogger(__name__)

# Discord hard-caps message content at 2000 chars.
_DISCORD_MAX_LEN = 2000


def send(text: str, webhook_url: str = "") -> None:
    """Fire-and-forget Discord webhook message.

    Each algorithm carries its own `webhook_url`; there is no global
    fallback, so an algorithm without one simply doesn't notify.
    """
    if not webhook_url:
        return
    url = webhook_url
    try:
        http.post(url, json={"content": text[:_DISCORD_MAX_LEN]}, timeout=5)
    except Exception as e:
        logger.warning("Discord send failed: %s", e)


# ---------------------------------------------------------------------------
# Canned message helpers
# ---------------------------------------------------------------------------
#
# Each helper accepts an optional `algo_name` so the message can be
# disambiguated when multiple algorithms run side-by-side.


def on_signal(intent, algo_name: str = "", webhook_url: str = "",
              paper: bool = False) -> None:
    """Generic detection notification — intent-based, works for any algorithm."""
    from .domain.intents import OpenIntent, CloseIntent, SettleIntent
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
    market_id = getattr(intent, "market_id", "") or ""
    url = _market_url(market_id)
    if url:
        lines.append(url)
    routed = threads.route(webhook_url, market_id, algo_name, paper)
    send("\n".join(lines), webhook_url=routed)


def on_trade_detected(trade: Trade, algo_name: str = "", webhook_url: str = "",
                      paper: bool = False) -> None:
    """Legacy entry point — kept for the wallet-watching CopyTrade flow that
    classifies a Trade before issuing an Intent. New algorithms should call
    `on_signal` instead."""
    url = _market_url(trade.market_id)
    routed = threads.route(webhook_url, trade.market_id, algo_name, paper)
    send(
        f"📥 **SIGNAL**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(trade.action)} `{_esc(trade.outcome)}`  "
        f"${trade.size_usdc:,.0f} @ **{trade.price:.3f}**"
        + (f"\n{url}" if url else ""),
        webhook_url=routed,
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
    url = _market_url(trade.market_id)
    text = (
        f"{tag}{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"`{_esc(trade.outcome)}`  ${spent_usdc:.2f} → {shares:.2f} shares "
        f"@ **{fill_price:.3f}**{slip}"
        + (f"\n{url}" if url else "")
    )
    existing_thread = threads.get(trade.market_id, algo_name, paper)
    if existing_thread:
        # Top-up: route into the existing thread
        send(text, webhook_url=f"{webhook_url}?thread_id={existing_thread}")
    elif config.DISCORD_BOT_TOKEN and webhook_url:
        # First fill and bot token available: post with wait=true and open a thread
        threads.open_thread(
            text, webhook_url, trade.market_id, algo_name, paper,
            thread_name=(trade.question or trade.market_id)[:100],
        )
    else:
        # No bot token or no webhook: plain send, no thread
        send(text, webhook_url=webhook_url)


def on_buy_failed(trade: Trade, reason: str, algo_name: str = "", webhook_url: str = "",
                  paper: bool = False) -> None:
    url = _market_url(trade.market_id)
    routed = threads.route(webhook_url, trade.market_id, algo_name, paper)
    send(
        f"❌ **BUY failed**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(reason)}"
        + (f"\n{url}" if url else ""),
        webhook_url=routed,
    )


def on_sell_executed(
    trade: Trade, shares: float, pnl: float, paper: bool, fill_price: float,
    algo_name: str = "", webhook_url: str = "",
) -> None:
    tag = "📄 **PAPER SELL**" if paper else "✅ **SELL**"
    sign = "+" if pnl >= 0 else ""
    url = _market_url(trade.market_id)
    routed = threads.route(webhook_url, trade.market_id, algo_name, paper)
    send(
        f"{tag}{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"`{_esc(trade.outcome)}`  {shares:.2f} shares @ **{fill_price:.3f}** · "
        f"P&L **{sign}${pnl:.2f}**"
        + (f"\n{url}" if url else ""),
        webhook_url=routed,
    )


def on_risk_blocked(reason: str, trade: Trade, algo_name: str = "", webhook_url: str = "",
                    paper: bool = False) -> None:
    url = _market_url(trade.market_id)
    routed = threads.route(webhook_url, trade.market_id, algo_name, paper)
    send(
        f"⚠️ **Risk block**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(reason)}"
        + (f"\n{url}" if url else ""),
        webhook_url=routed,
    )


def on_settle_executed(
    market_id: str,
    question: str,
    outcome: str,
    shares: float,
    proceeds: float,
    pnl: float,
    close_price: float,
    paper: bool,
    algo_name: str = "",
    webhook_url: str = "",
) -> None:
    """Settlement notification — routes to the position's thread if one exists."""
    sign = "+" if pnl >= 0 else ""
    result = "WIN" if close_price > 0.5 else "LOSS"
    tag = "📄 **PAPER SETTLE**" if paper else ("✅ **WIN**" if result == "WIN" else "❌ **LOSS**")
    url = _market_url(market_id)
    routed = threads.route(webhook_url, market_id, algo_name, paper)
    send(
        f"{tag}{_subtitle(algo_name)}\n"
        f"**{_esc(question[:80])}**\n"
        f"`{_esc(outcome)}` resolved | {shares:.2f} shares "
        f"→ **${proceeds:.2f}** | P&L **{sign}${pnl:.2f}**"
        + (f"\n{url}" if url else ""),
        webhook_url=routed,
    )


def on_startup(mode: str, exposure: float, algo_name: str = "", webhook_url: str = "") -> None:
    send(
        f"🚀 **Bot started**{_subtitle(algo_name)} — {_esc(mode)} mode\n"
        f"Exposure: **${exposure:.2f}**",
        webhook_url=webhook_url,
    )


def on_shutdown(algo_name: str = "", webhook_url: str = "") -> None:
    send(f"🛑 **Bot stopped**{_subtitle(algo_name)}", webhook_url=webhook_url)


def on_worker_unstable(
    algo_name: str,
    error_count: int,
    last_exc: BaseException,
    webhook_url: str = "",
) -> None:
    """Alert when a worker's poll loop has failed repeatedly."""
    send(
        f"🚨 **Worker unstable**{_subtitle(algo_name)}\n"
        f"> {error_count} consecutive poll failures\n"
        f"> {_esc(str(last_exc)[:200])}",
        webhook_url=webhook_url,
    )


def on_heartbeat(
    algo_infos: list,
    webhook_url: str,
    profile: str = "",
) -> None:
    """Post a compact liveness ping with per-algo exposure.

    `algo_infos` is a list of (algo_name, paper) tuples — same shape as
    `send_profile_summary`. Fresh PositionTracker instances are created
    so this can run from any thread.
    """
    if not webhook_url:
        return

    from bot.positions import PositionTracker

    now_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
    profile_label = _esc(profile) if profile else "all"
    lines = [f"💓 **Heartbeat · {profile_label} · {now_str}**"]

    for name, paper in algo_infos:
        tracker = PositionTracker(algo=name)
        exposure = tracker.total_exposure_usdc(paper=paper)
        mode_tag = "PAPER" if paper else "LIVE"
        lines.append(f"> {_esc(name)} · {mode_tag} · ${exposure:.2f} exposure")

    send("\n".join(lines), webhook_url=webhook_url)


def start_heartbeat(
    algo_infos: list,
    webhook_url: str,
    stop_event: threading.Event | None = None,
    profile: str = "",
    interval_hours: float = 6.0,
) -> None:
    """Post a heartbeat ping every `interval_hours` hours.

    The first ping fires after one full interval (not immediately on boot —
    that would duplicate the startup message). Pass `interval_hours=0` to
    disable.
    """
    if not webhook_url or interval_hours <= 0:
        return

    interval_s = interval_hours * 3600

    def _loop() -> None:
        while True:
            if stop_event is not None:
                if stop_event.wait(interval_s):
                    return
            else:
                threading.Event().wait(interval_s)
            try:
                on_heartbeat(algo_infos, webhook_url, profile)
            except Exception:
                logger.exception("Heartbeat raised, continuing")

    t = threading.Thread(target=_loop, daemon=True, name="heartbeat")
    t.start()
    logger.info(
        "Heartbeat thread started (profile=%s, interval=%.1fh).",
        profile or "?", interval_hours,
    )


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


def _seconds_until_next_sunday() -> float:
    """Time until the next Sunday midnight (start of day) in config timezone.

    Always schedules at least one week out so the thread fires once per week
    even if started on a Sunday.
    """
    now = datetime.now(tz=config.TIMEZONE)
    days_ahead = (6 - now.weekday()) % 7  # 0 if today is Sunday
    if days_ahead == 0:
        days_ahead = 7
    next_sunday = (now + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1.0, (next_sunday - now).total_seconds())


# ---------------------------------------------------------------------------
# Weekly signal performance digest
# ---------------------------------------------------------------------------

def send_weekly_signal_digest(
    algo_infos: list,
    webhook_url: str,
    profile: str = "",
) -> None:
    """Post a 7-day signal performance summary to the summary channel.

    Reads directly from the `signals` table — one section per algorithm,
    plus a combined total. Only settled positions (pnl_usdc IS NOT NULL)
    contribute to win rate and P&L stats.

    `algo_infos` is a list of (algo_name, paper) tuples — the same shape
    used by `send_profile_summary`.
    """
    if not webhook_url:
        return

    from bot import db

    week_start_ts = int(time.time()) - 7 * 86_400
    now = datetime.now(tz=config.TIMEZONE)
    week_end_str = now.strftime("%Y-%m-%d")
    week_start_str = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    profile_label = _esc(profile) if profile else "all"

    lines = [
        f"📈 **Weekly Signal Digest · {week_start_str} – {week_end_str} · {profile_label}**",
        "",
    ]

    total_week_pnl = 0.0
    any_data = False

    for name, paper in algo_infos:
        row = db.get().execute(
            """
            SELECT
                COUNT(*)                                         AS executed_count,
                COUNT(pnl_usdc)                                  AS labeled_count,
                COUNT(CASE WHEN pnl_usdc > 0 THEN 1 END)        AS wins,
                AVG(signal_price)                                AS avg_entry_price,
                AVG(pnl_usdc)                                    AS avg_pnl,
                COALESCE(SUM(pnl_usdc), 0)                      AS total_pnl
            FROM signals
            WHERE executed = 1
              AND algo  = ?
              AND paper = ?
              AND ts   >= ?
            """,
            (name, 1 if paper else 0, week_start_ts),
        ).fetchone()

        executed  = row["executed_count"]  or 0
        labeled   = row["labeled_count"]   or 0
        wins      = int(row["wins"]        or 0)
        avg_entry = row["avg_entry_price"]
        avg_pnl   = row["avg_pnl"]
        total_pnl = float(row["total_pnl"] or 0.0)
        total_week_pnl += total_pnl

        mode_tag = "PAPER" if paper else "LIVE"
        if executed > 0:
            any_data = True

        lines.append(f"**{_esc(name)}** · {mode_tag}")
        lines.append(f"> Signals: {executed} fired · {labeled} settled")

        if labeled > 0:
            win_pct = wins / labeled * 100
            win_emoji = "🟢" if win_pct >= 50 else "🔴"
            avg_entry_str = f"{avg_entry:.3f}" if avg_entry is not None else "—"
            avg_pnl_val = avg_pnl or 0.0
            avg_pnl_str = f"{'+' if avg_pnl_val >= 0 else ''}${avg_pnl_val:.2f}"
            pnl_sign = "+" if total_pnl >= 0 else ""
            pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(f"> Win rate: {win_emoji} **{win_pct:.0f}%** ({wins}/{labeled})")
            lines.append(f"> Avg entry: {avg_entry_str} · Avg P&L/trade: {avg_pnl_str}")
            lines.append(f"> Week P&L: {pnl_emoji} **{pnl_sign}${total_pnl:.2f}**")
        elif executed > 0:
            lines.append("> _(No settled positions this week yet)_")
        else:
            lines.append("> _(No signals fired this week)_")

        lines.append("")

    if not any_data:
        lines.append("_No signals fired across any algorithm this week._")
    else:
        pnl_sign = "+" if total_week_pnl >= 0 else ""
        pnl_emoji = "🟢" if total_week_pnl >= 0 else "🔴"
        algo_word = "algorithm" if len(algo_infos) == 1 else "algorithms"
        lines.append("─" * 22)
        lines.append(f"**Total · {len(algo_infos)} {algo_word}**")
        lines.append(f"> {pnl_emoji} Week P&L: **{pnl_sign}${total_week_pnl:.2f}**")

    send("\n".join(lines), webhook_url=webhook_url)


def start_weekly_digest(
    algo_infos: list,
    webhook_url: str,
    stop_event: threading.Event | None = None,
    profile: str = "",
) -> None:
    """Start a background thread that posts the weekly signal digest every Sunday.

    Fires at the next Sunday midnight (config.TIMEZONE), then repeats weekly.
    Uses the same `algo_infos` / `webhook_url` shape as the daily summary.
    """
    if not webhook_url:
        return

    def _loop() -> None:
        while True:
            wait_s = _seconds_until_next_sunday()
            if stop_event is not None:
                if stop_event.wait(wait_s):
                    return
            else:
                threading.Event().wait(wait_s)
            try:
                send_weekly_signal_digest(algo_infos, webhook_url, profile)
            except Exception:
                logger.exception("Weekly signal digest raised, continuing")

    t = threading.Thread(target=_loop, daemon=True, name="weekly-digest")
    t.start()
    logger.info("Weekly signal digest thread started (profile=%s).", profile or "?")
