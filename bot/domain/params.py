"""
params.py

How an algorithm is configured: its execution mode, and the parameter
surface the runner and risk gate read. Each strategy's own params
dataclass satisfies AlgoParams structurally — no inheritance.
"""

from enum import Enum
from typing import Protocol


class Mode(str, Enum):
    """Execution mode selected independently for each algorithm."""

    PAPER = "paper"
    LIVE = "live"


class AlgoParams(Protocol):
    """The parameter surface shared by execution and risk policy."""

    name: str
    mode: Mode
    max_position_size_usdc: float
    max_total_exposure_usdc: float
    daily_loss_limit_usdc: float
    min_order_size_usdc: float
    max_slippage: float
    poll_interval_seconds: int
    paper_starting_balance: float
    order_type: str
    paper_fee_bps: float
    webhook_url: str
