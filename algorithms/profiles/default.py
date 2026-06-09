"""
Default profile — boots the env-driven CopyTradeAlgorithm in whatever mode
its params resolve to (paper unless COPYTRADE_MODE / PAPER_TRADE says otherwise).

Used when no PROFILE is set. Suitable for development and the simple
"one algorithm, env-configured" deployment.
"""

from algorithms.copy_trade import CopyTradeAlgorithm

ALGORITHMS = [
    CopyTradeAlgorithm(),
]
