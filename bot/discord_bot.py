"""
Discord bot — two-way interactive interface.

Runs as a daemon thread alongside trading workers. Exposes slash commands
so you can query bot state from Discord without SSH:

  /status       — all algorithms: mode, exposure, today's P&L
  /positions    — open positions (optional algo name filter)
  /pnl          — P&L breakdown per algo + combined total
  /summary      — send the daily summary to the summary channel right now

Two bots (prod + experimental) can coexist in the same Discord server —
Discord disambiguates them by application name in the slash-command picker.

Setup (one-time):
  1. discord.com/developers → New Application → Bot → Reset Token → copy it
  2. OAuth2 → URL Generator → scopes: bot + applications.commands
     → bot permissions: Send Messages, Use Slash Commands → invite URL
  3. Set DISCORD_BOT_TOKEN in .env
  4. Optionally set DISCORD_GUILD_ID (right-click server → Copy Server ID with
     Developer Mode on) — guild-scoped commands appear instantly; global
     commands can take up to 1 hour to propagate.
"""

import asyncio
import logging
import threading
from datetime import datetime

import discord
from discord import app_commands

from . import config, notifier
from .positions import PositionTracker

logger = logging.getLogger(__name__)

# Set by start() before the bot thread launches.
_algo_infos: list = []   # [(name: str, paper: bool)]
_profile: str = ""


# ---------------------------------------------------------------------------
# Response builders — pure functions, no async, safe from any thread
# ---------------------------------------------------------------------------

def _status_text() -> str:
    today = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d %H:%M")
    lines = [f"🤖 **Bot Status · {notifier._esc(_profile)} · {today}**", ""]
    for name, paper in _algo_infos:
        tracker = PositionTracker(algo=name)
        n_pos = len(tracker.all_open(paper=paper))
        exposure = tracker.total_exposure_usdc(paper=paper)
        pnl = tracker.today_pnl_usdc(paper=paper)
        sign = "+" if pnl >= 0 else ""
        mode = "📄 PAPER" if paper else "🟢 LIVE"
        lines.append(
            f"**{notifier._esc(name)}** · {mode}\n"
            f"> {n_pos} open · **${exposure:.2f}** exposure · today **{sign}${pnl:.2f}**"
        )
    return "\n".join(lines)


def _positions_text(algo_filter: str = "") -> str:
    lines = []
    for name, paper in _algo_infos:
        if algo_filter and algo_filter.lower() not in name.lower():
            continue
        tracker = PositionTracker(algo=name)
        positions = tracker.all_open(paper=paper)
        mode = "PAPER" if paper else "LIVE"
        lines.append(f"📋 **{notifier._esc(name)}** · {mode}")
        if not positions:
            lines.append("> _No open positions_")
        else:
            for p in positions:
                lines.append(
                    f"> `{notifier._esc(p.outcome)}` {p.shares:.2f}sh"
                    f" @ **{p.avg_price:.3f}**  ·  ${p.total_cost_usdc:.2f} cost"
                    f"  ·  {notifier._esc(p.question[:55])}"
                )
        lines.append("")
    return "\n".join(lines).strip() or "_No matching algorithms._"


def _pnl_text() -> str:
    today = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d")
    lines = [f"💰 **P&L Summary · {today}**", ""]
    total_pnl = 0.0
    total_exp = 0.0
    for name, paper in _algo_infos:
        tracker = PositionTracker(algo=name)
        pnl = tracker.today_pnl_usdc(paper=paper)
        exp = tracker.total_exposure_usdc(paper=paper)
        total_pnl += pnl
        total_exp += exp
        sign = "+" if pnl >= 0 else ""
        mode = "PAPER" if paper else "LIVE"
        lines.append(
            f"**{notifier._esc(name)}** ({mode}): "
            f"today **{sign}${pnl:.2f}** · exposure **${exp:.2f}**"
        )
    sign = "+" if total_pnl >= 0 else ""
    lines += [
        "",
        "─" * 22,
        f"**Combined**: today **{sign}${total_pnl:.2f}** · exposure **${total_exp:.2f}**",
    ]
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
            logger.info("Discord slash commands synced globally (may take up to 1h to appear).")

    async def on_ready(self) -> None:
        logger.info("Discord bot ready: %s (id=%s)", self.user, self.user.id)


def _register_commands(client: _TradingClient) -> None:
    @client.tree.command(
        name="status",
        description="Show all algorithms: mode, exposure, and today's P&L",
    )
    async def status_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(_status_text())

    @client.tree.command(
        name="positions",
        description="List open positions",
    )
    @app_commands.describe(algo="Filter by algorithm name (optional, partial match)")
    async def positions_cmd(interaction: discord.Interaction, algo: str = "") -> None:
        await interaction.response.send_message(_positions_text(algo))

    @client.tree.command(
        name="pnl",
        description="Today's realized P&L and open exposure by algorithm",
    )
    async def pnl_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(_pnl_text())

    @client.tree.command(
        name="summary",
        description="Send the daily summary to the summary channel right now",
    )
    async def summary_cmd(interaction: discord.Interaction) -> None:
        webhook = config.resolve_summary_webhook(_profile)
        if not webhook:
            await interaction.response.send_message(
                "⚠️ No summary webhook configured for this profile.", ephemeral=True
            )
            return
        notifier.send_profile_summary(_algo_infos, webhook, _profile)
        await interaction.response.send_message("✅ Summary sent.", ephemeral=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def start(algo_infos: list, profile: str) -> None:
    """Launch the Discord bot in a daemon thread. No-op if token is not set."""
    global _algo_infos, _profile
    _algo_infos = algo_infos
    _profile = profile

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
    logger.info("Discord bot thread started (profile=%s).", profile)
