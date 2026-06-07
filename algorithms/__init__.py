"""
Algorithm registry.

`ENABLED` lists the algorithms that get started by `main.py`. Each runs in
its own worker thread with its own poll cadence, tracker, risk pool, and
paper bankroll.

To add a new strategy:
  1. Create `algorithms/<your_algo>/{__init__.py, algorithm.py, params.py}`
  2. Import its class here and append an instance to `ENABLED`.
"""

from .copy_trade import CopyTradeAlgorithm

ENABLED = [
    CopyTradeAlgorithm(),
    # MeanReversionAlgorithm(),   # example future addition
]

__all__ = ["ENABLED", "CopyTradeAlgorithm"]
