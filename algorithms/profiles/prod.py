"""
Prod profile — algorithms that trade with real money.

Loaded when `PROFILE=prod` (which also makes config.py prefer `.env.prod`).
Keep this list short and conservative. Every algorithm here must be
explicitly `Mode.LIVE` — a typo defaulting to PAPER while you think you're
live is a silent failure mode, while the reverse is harmless.
"""

from dataclasses import replace

from algorithms.copy_trade import CopyTradeAlgorithm, CopyTradeParams
from bot.algorithm import Mode

_BASE = CopyTradeParams()

ALGORITHMS = [
    CopyTradeAlgorithm(
        params=replace(_BASE, name="copy_trade_prod", mode=Mode.LIVE),
    ),
]
