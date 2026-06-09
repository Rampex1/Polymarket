"""
Experimental profile — paper-mode algorithms you're tuning or A/B testing.

Loaded when `PROFILE=experimental`. Use this for trying out new params,
new sizing tiers, or new algorithms before promoting them to `prod`.
Everything here should be `Mode.PAPER` — there are no Polymarket creds in
.env.experimental (intentionally).

Example: two CopyTrade variants with different tier sizes, both paper.
"""

from dataclasses import replace

from algorithms.copy_trade import CopyTradeAlgorithm, CopyTradeParams
from bot.algorithm import Mode

_BASE = CopyTradeParams()

ALGORITHMS = [
    CopyTradeAlgorithm(
        params=replace(
            _BASE,
            name="copy_trade_paper_baseline",
            mode=Mode.PAPER,
        ),
    ),
    # Example variant — larger tiers, higher per-position cap. Comment out
    # if you only want one variant running.
    CopyTradeAlgorithm(
        params=replace(
            _BASE,
            name="copy_trade_paper_2x",
            mode=Mode.PAPER,
            tier1_size=2.0,
            tier2_size=4.0,
            tier3_size=6.0,
            max_position_size_usdc=6.0,
            max_total_exposure_usdc=24.0,
        ),
    ),
]
