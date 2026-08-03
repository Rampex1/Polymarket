"""Strategy base class and compatibility exports.

The domain command types moved to :mod:`bot.domain.intents`.  They remain
available here so third-party strategies and existing profile code do not
need a flag-day migration.
"""

from abc import ABC, abstractmethod
from typing import Iterator

from .domain.intents import (
    AlgoParams,
    CloseIntent,
    Intent,
    Mode,
    OpenIntent,
    SettleIntent,
)


class Algorithm(ABC):
    """A stateful strategy that emits side-effect-free execution intents."""

    params: AlgoParams

    def setup(self, tracker, notifier_mod, client) -> None:
        """Receive worker-scoped dependencies once before polling begins."""

    @abstractmethod
    def poll(self) -> Iterator[Intent]:
        """Yield zero or more intents for one worker tick."""

    @property
    def name(self) -> str:
        return self.params.name

    @property
    def display_name(self) -> str:
        return self.name


__all__ = [
    "Algorithm", "AlgoParams", "CloseIntent", "Intent", "Mode",
    "OpenIntent", "SettleIntent",
]
