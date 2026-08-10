"""
discord_bot.py

Slash-command bot; one standalone daemon serves every profile.
Wiring only — what each command *says* lives in views.py.

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
from pathlib import Path

import discord
from discord import app_commands

from .. import config
from . import views
from .messages import escape as _esc
from .webhook import send

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        await interaction.response.send_message(views._status_text())

    @client.tree.command(
        name="positions",
        description="List open positions across all profiles",
    )
    @app_commands.describe(algo="Filter by algorithm name (optional, partial match)")
    async def positions_cmd(interaction: discord.Interaction, algo: str = "") -> None:
        await interaction.response.send_message(views._positions_text(algo))

    @client.tree.command(
        name="pnl",
        description="Today's realized P&L and open exposure, all algorithms combined",
    )
    async def pnl_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(views._pnl_text())

    @client.tree.command(
        name="summary",
        description="Post a portfolio summary to all profile summary channels",
    )
    async def summary_cmd(interaction: discord.Interaction) -> None:
        sent = []
        for profile, algos in views._by_profile().items():
            webhook = config.resolve_summary_webhook(profile)
            if webhook:
                send(views._summary_text(algos, profile), webhook_url=webhook)
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
    views.set_algos(algo_infos)

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
