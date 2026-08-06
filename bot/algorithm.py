"""Strategy base class.

The domain types — intents, `Mode`, `AlgoParams` — live in
:mod:`bot.domain.intents` and are imported from there. This module only
defines the class a strategy subclasses.
"""

from abc import ABC, abstractmethod
from typing import Iterator

from .domain.intents import AlgoParams, Intent


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


__all__ = ["Algorithm"]
