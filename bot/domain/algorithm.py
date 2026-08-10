"""
algorithm.py

The contract every trading strategy implements.
A strategy polls for opportunities and yields intents; the runner executes them.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Iterator

from .intents import Intent
from .params import AlgoParams

if TYPE_CHECKING:  # annotation only — domain must not import storage at runtime
    from ..storage.ledger import Ledger


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
