"""
Algorithm registry — profile-driven.

The `PROFILE` env var selects which bundle of algorithms this process runs.
The profile module under `algorithms.profiles.<name>` must export a list
called `ALGORITHMS`. Each entry runs in its own worker thread with its own
poll cadence, tracker, risk pool, and paper bankroll.

Examples:
    PROFILE=prod          → algorithms/profiles/prod.py
    PROFILE=experimental  → algorithms/profiles/experimental.py
    PROFILE unset         → algorithms/profiles/default.py

To add a new strategy:
  1. Create `algorithms/<your_algo>/{__init__.py, algorithm.py, params.py}`.
  2. Import its class in whichever profile(s) should run it.
"""

import os
from importlib import import_module

from .copy_trade import CopyTradeAlgorithm

_profile_name = os.getenv("PROFILE", "default")

try:
    _profile = import_module(f"algorithms.profiles.{_profile_name}")
except ModuleNotFoundError as e:
    raise RuntimeError(
        f"Profile '{_profile_name}' not found at algorithms/profiles/{_profile_name}.py. "
        f"Create the file or set PROFILE to an existing one."
    ) from e

ENABLED = _profile.ALGORITHMS

__all__ = ["ENABLED", "CopyTradeAlgorithm"]
