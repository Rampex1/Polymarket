"""
algorithm.py

The contract every trading strategy implements: the ABC itself, and the
parameter surface execution reads off it. A strategy polls for
opportunities and yields intents; the runner executes them.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Iterator, Protocol

from .intents import Intent
from .mode import Mode

if TYPE_CHECKING:  # annotation only — domain must not import storage at runtime
    from ..storage.ledger import Ledger


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


class Algorithm(ABC):
    """Base class for a trading strategy"""

    params: AlgoParams

    def setup(self, ledger: "Ledger") -> None:
        """One-time startup before the first poll"""

    @abstractmethod
    def poll(self) -> Iterator[Intent]:
        """Yield zero or more intents for one worker tick."""

    @property
    def name(self) -> str:
        return self.params.name

    @property
    def display_name(self) -> str:
        return self.name
