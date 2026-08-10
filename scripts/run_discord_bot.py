#!/usr/bin/env python3
"""
Standalone Discord bot — serves all profiles from one process.

Loads every available profile TOML, registers all algorithms, and starts
the bot. Runs in its own tmux session (discord) separate from the trading
workers so it stays up even if a worker crashes, and a single bot token
covers all profiles.

Usage:
    source .venv/bin/activate
    python scripts/run_discord_bot.py
"""

import logging
import os
import signal
import sys
import threading

# Run from repo root so config/ and bot/ are importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

from algorithms import REGISTRY
from bot.discord import discord_bot
from bot.domain.mode import Mode
from bot.profile_loader import ProfileError, available_profiles, load_profile

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
    logger.error("No algorithms found across any profile — nothing to serve.")
    sys.exit(1)

discord_bot.start_standalone(algo_infos)

# Keep the process alive — the bot runs in a daemon thread.
stop = threading.Event()
signal.signal(signal.SIGINT, lambda *_: stop.set())
signal.signal(signal.SIGTERM, lambda *_: stop.set())
logger.info("Discord bot running. Ctrl-C or SIGTERM to stop.")
stop.wait()
logger.info("Shutting down.")
