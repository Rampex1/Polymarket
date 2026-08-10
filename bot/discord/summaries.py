"""
summaries.py

Profile-level digests to the shared summary channel: the on-demand /summary,
the liveness heartbeat, and the Sunday signal digest. All three take the same
(algo_name, paper) list and build fresh Ledgers, so they run from any thread.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from .. import config
from .messages import escape as _esc
from .webhook import send

logger = logging.getLogger(__name__)


def on_heartbeat(
    algo_infos: list,
    webhook_url: str,
    profile: str = "",
) -> None:
    """Post a compact liveness ping with per-algo exposure.

    `algo_infos` is a list of (algo_name, paper) tuples — same shape as
    `send_profile_summary`. Fresh Ledger instances are created
    so this can run from any thread.
    """
    if not webhook_url:
        return

    from bot.storage.ledger import Ledger

    now_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
    profile_label = _esc(profile) if profile else "all"
    lines = [f"💓 **Heartbeat · {profile_label} · {now_str}**"]

    for name, paper in algo_infos:
        ledger = Ledger(algo=name)
        exposure = ledger.total_exposure_usdc(paper=paper)
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


def send_profile_summary(
    algo_infos: list,
    webhook_url: str,
    profile: str = "",
) -> None:
    """Profile-level summary — one message per profile to a shared channel.

    Sent on demand only (`/summary`); there is no scheduled version.

    `algo_infos` is a list of (algo_name, paper) tuples. Fresh Ledger
    instances are created per algo so this can run from any thread without
    holding live ledger references.

    Layout:
      📊 **Summary · DATE · PROFILE**

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

    from bot.storage.ledger import Ledger

    today_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d")
    profile_label = _esc(profile) if profile else "all"

    lines = [f"📊 **Summary · {today_str} · {profile_label}**", ""]

    total_pnl = 0.0
    total_exposure = 0.0

    for name, paper in algo_infos:
        ledger = Ledger(algo=name)
        positions = ledger.all_open(paper=paper)
        exposure = ledger.total_exposure_usdc(paper=paper)
        pnl = ledger.today_pnl_usdc(paper=paper)
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

    from bot.storage import db

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
