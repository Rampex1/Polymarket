"""
discord_bot.py

Slash-command bot; one standalone daemon serves every profile.

/status    — every algorithm by profile: mode, exposure, today's P&L
/positions — open positions, optional algo name filter
/pnl       — today's realized P&L + open exposure, combined
/summary   — post a portfolio summary to every profile channel (on demand only)
/restart   — git pull + restart the VPS sessions (admin only)
"""

import asyncio
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

from .. import config
from .messages import escape as _esc
from .webhook import send
from ..storage.ledger import Ledger

logger = logging.getLogger(__name__)

_algo_infos: list = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Discord client + slash commands
# ---------------------------------------------------------------------------


class _TradingClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild_id_str = config.DISCORD_GUILD_ID
        if guild_id_str:
            guild = discord.Object(id=int(guild_id_str))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Discord slash commands registered to guild %s.", guild_id_str)
        else:
            await self.tree.sync()
            logger.info(
                "Discord slash commands synced globally (may take up to 1h to appear)."
            )

    async def on_ready(self) -> None:
        logger.info("Discord bot ready: %s (id=%s)", self.user, self.user.id)


def _register_commands(client: _TradingClient) -> None:
    @client.tree.command(
        name="status",
        description="Show all algorithms grouped by profile: mode, exposure, today's P&L",
    )
    async def status_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(_status_text())

    @client.tree.command(
        name="positions",
        description="List open positions across all profiles",
    )
    @app_commands.describe(algo="Filter by algorithm name (optional, partial match)")
    async def positions_cmd(interaction: discord.Interaction, algo: str = "") -> None:
        await interaction.response.send_message(_positions_text(algo))

    @client.tree.command(
        name="pnl",
        description="Today's realized P&L and open exposure, all algorithms combined",
    )
    async def pnl_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(_pnl_text())

    @client.tree.command(
        name="summary",
        description="Post a portfolio summary to all profile summary channels",
    )
    async def summary_cmd(interaction: discord.Interaction) -> None:
        sent = []
        for profile, algos in _by_profile().items():
            webhook = config.resolve_summary_webhook(profile)
            if webhook:
                send(_summary_text(algos, profile), webhook_url=webhook)
                sent.append(profile)
        if sent:
            await interaction.response.send_message(
                f"✅ Summary sent for: {', '.join(sent)}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ No summary webhooks configured for any profile.", ephemeral=True
            )

    @client.tree.command(
        name="restart",
        description="git pull + restart all bot sessions on the VPS (admin only)",
    )
    async def restart_cmd(interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Administrator permission required.", ephemeral=True
            )
            return

        requester = _esc(interaction.user.display_name)
        await interaction.response.send_message(
            f"⏳ **Restarting…** _(requested by {requester})_\n"
            "> Pulling latest code, installing deps, validating profiles.\n"
            "> All sessions will go offline briefly — watch for the startup messages."
        )

        # Delay so the response lands in Discord before the process is killed
        # by setup_vm.sh restarting the discord tmux session.
        async def _run() -> None:
            await asyncio.sleep(1)
            script = Path(__file__).parent.parent / "scripts" / "setup_vm.sh"
            subprocess.Popen(["bash", str(script)], cwd=str(script.parent.parent))

        asyncio.create_task(_run())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def start_standalone(algo_infos: list) -> None:
    """Launch the bot in a daemon thread.

    `algo_infos` is a list of (algo_name, paper, profile) triples collected
    from all loaded profiles. No-op if DISCORD_BOT_TOKEN is not set.
    """
    global _algo_infos
    _algo_infos = algo_infos

    if not config.DISCORD_BOT_TOKEN:
        logger.info("DISCORD_BOT_TOKEN not set — Discord bot disabled.")
        return

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = _TradingClient()
        _register_commands(client)
        try:
            loop.run_until_complete(client.start(config.DISCORD_BOT_TOKEN))
        except Exception:
            logger.exception("Discord bot exited with error.")
        finally:
            loop.close()

    threading.Thread(target=_run, daemon=True, name="discord-bot").start()
    logger.info(
        "Discord bot thread started (%d algo(s) across %d profile(s)).",
        len(algo_infos),
        len({p for _, _, p in algo_infos}),
    )
