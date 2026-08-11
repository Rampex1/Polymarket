#!/usr/bin/env python3
"""
__main__.py

Entry point for the Discord bot: `python -m bot.discord`.

Serves every profile from one process, in its own tmux session, separate
from the trading workers — a worker crash leaves the bot answering, and a
bot crash leaves the workers trading. One token covers all profiles.
"""

import logging
import signal
import threading

from algorithms import REGISTRY
from bot.discord import discord_bot
from bot.domain.mode import Mode
from bot.profile_loader import ProfileError, available_profiles, load_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

algo_infos = []
for profile_name in available_profiles():
    try:
        algos = load_profile(profile_name, REGISTRY)
        for algo in algos:
            paper = algo.params.mode == Mode.PAPER
            algo_infos.append((algo.params.name, paper, profile_name))
            logger.info(
                "Registered: %s (%s) from profile=%s",
                algo.params.name, "PAPER" if paper else "LIVE", profile_name,
            )
    except ProfileError as e:
        logger.warning("Skipping profile %s: %s", profile_name, e)

if not algo_infos:
    raise SystemExit("No algorithms found across any profile — nothing to serve.")

discord_bot.start_standalone(algo_infos)

# Keep the process alive — the bot runs in a daemon thread.
stop = threading.Event()
signal.signal(signal.SIGINT, lambda *_: stop.set())
signal.signal(signal.SIGTERM, lambda *_: stop.set())
logger.info("Discord bot running. Ctrl-C or SIGTERM to stop.")
stop.wait()
logger.info("Shutting down.")
