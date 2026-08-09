"""
Algorithm registry — declarative, config-driven profiles.

The `PROFILE` env var selects `config/<profile>.toml`, which declares the
bundle of algorithms this process runs (see bot/profile_loader.py for the
file shape). Each entry runs in its own worker thread with its own poll
cadence, tracker, risk pool, and paper bankroll.

Examples:
    PROFILE=prod          → config/prod.toml
    PROFILE=experimental  → config/experimental.toml
    PROFILE unset         → refuses to boot — running a trading bot under
                            an implicit profile is how paper config ends
                            up trading real money (or vice versa)

To add a new strategy:
  1. Create `algorithms/<your_algo>/{__init__.py, algorithm.py, params.py}`.
  2. Register it in REGISTRY below.
  3. Reference it by type in whichever config/<profile>.toml should run it.

`ENABLED` is resolved lazily (PEP 562 module __getattr__) so tooling that
only needs REGISTRY can import this package
without requiring a valid profile file.
"""

import os

from bot.profile_loader import ProfileError, available_profiles, load_profile

from .copy_trade import CopyTradeAlgorithm, CopyTradeParams
from .insider_flow import InsiderFlowAlgorithm, InsiderFlowParams

# type string (used in config TOML) → (AlgorithmCls, ParamsCls)
REGISTRY = {
    "copy_trade": (CopyTradeAlgorithm, CopyTradeParams),
    "insider_flow": (InsiderFlowAlgorithm, InsiderFlowParams),
}

PROFILE = os.getenv("PROFILE")

_enabled = None


def __getattr__(name):
    if name == "ENABLED":
        global _enabled
        if _enabled is None:
            if not PROFILE:
                avail = ", ".join(available_profiles()) or "none found"
                raise ProfileError(
                    "PROFILE is not set — refusing to guess which config to "
                    "trade with. Run with an explicit profile, e.g. "
                    "PROFILE=experimental (paper) or PROFILE=prod (live). "
                    f"Available: {avail}."
                )
            _enabled = load_profile(PROFILE, REGISTRY)
        return _enabled
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ENABLED", "REGISTRY", "CopyTradeAlgorithm", "InsiderFlowAlgorithm"]
