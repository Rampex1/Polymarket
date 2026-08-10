"""
views.py

What each slash command says — one builder per command, named for it.
Pure functions: no async, no discord library, safe to call from any thread,
so the wording can be tested without a gateway connection.
"""

from datetime import datetime

from .. import config
from ..storage.ledger import Ledger
from .messages import escape as _esc

# Populated by discord_bot.start_standalone: (algo_name, paper, profile).
_algo_infos: list = []


def set_algos(algo_infos: list) -> None:
    """Register the algorithms every builder reports on."""
    _algo_infos[:] = algo_infos


def _by_profile() -> dict:
    """Group _algo_infos by profile, preserving insertion order."""
    result: dict[str, list] = {}
    for name, paper, profile in _algo_infos:
        result.setdefault(profile, []).append((name, paper))
    return result


# ---------------------------------------------------------------------------
# Response builders — one per command, pure, no async, safe from any thread.
# Each is named for the command it serves: /status -> _status_text, and so on.
# ---------------------------------------------------------------------------


def _status_text() -> str:
    """/status — every algorithm by profile: mode, open count, exposure, P&L."""
    today = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d %H:%M")
    lines = [f"🤖 **Bot Status · {today}**", ""]
    for profile, algos in _by_profile().items():
        lines.append(f"**{_esc(profile)}**")
        for name, paper in algos:
            ledger = Ledger(algo=name)
            n_pos = len(ledger.all_open(paper=paper))
            exposure = ledger.total_exposure_usdc(paper=paper)
            pnl = ledger.today_pnl_usdc(paper=paper)
            sign = "+" if pnl >= 0 else ""
            mode = "📄 PAPER" if paper else "🟢 LIVE"
            lines.append(
                f"> **{_esc(name)}** · {mode} · "
                f"{n_pos} open · **${exposure:.2f}** · today **{sign}${pnl:.2f}**"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _positions_text(algo_filter: str = "") -> str:
    """/positions — every open position, optionally filtered by algo name."""
    lines = []
    for name, paper, profile in _algo_infos:
        if algo_filter and algo_filter.lower() not in name.lower():
            continue
        ledger = Ledger(algo=name)
        positions = ledger.all_open(paper=paper)
        mode = "PAPER" if paper else "LIVE"
        lines.append(
            f"📋 **{_esc(name)}** · {mode} · {_esc(profile)}"
        )
        if not positions:
            lines.append("> _No open positions_")
        else:
            for p in positions:
                lines.append(
                    f"> `{_esc(p.outcome)}` {p.shares:.2f}sh"
                    f" @ **{p.avg_price:.3f}**  ·  ${p.total_cost_usdc:.2f} cost"
                    f"  ·  {_esc(p.question[:55])}"
                )
        lines.append("")
    return "\n".join(lines).strip() or "_No matching algorithms._"


def _pnl_text() -> str:
    """/pnl — today's realized P&L and exposure per algo, plus the total."""
    today = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d")
    lines = [f"💰 **P&L Summary · {today}**", ""]
    total_pnl = 0.0
    total_exp = 0.0
    for name, paper, profile in _algo_infos:
        ledger = Ledger(algo=name)
        pnl = ledger.today_pnl_usdc(paper=paper)
        exp = ledger.total_exposure_usdc(paper=paper)
        total_pnl += pnl
        total_exp += exp
        sign = "+" if pnl >= 0 else ""
        mode = "PAPER" if paper else "LIVE"
        lines.append(
            f"**{_esc(name)}** ({mode} · {_esc(profile)}): "
            f"today **{sign}${pnl:.2f}** · exposure **${exp:.2f}**"
        )
    sign = "+" if total_pnl >= 0 else ""
    lines += [
        "",
        "─" * 22,
        f"**Combined**: today **{sign}${total_pnl:.2f}** · exposure **${total_exp:.2f}**",
    ]
    return "\n".join(lines)


def _summary_text(
    algo_infos: list,
    profile: str = "",
) -> str:
    """/summary — one profile's full portfolio summary.

    Unlike the other builders this is posted to the profile's summary
    webhook channel rather than returned to the caller, so it reads the
    same whether a human asked for it or (some day) a scheduler does.
    Fresh Ledgers, so any thread can call it.

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

    return "\n".join(lines)
