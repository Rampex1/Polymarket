from abc import ABC, abstractmethod
from typing import Iterator

from .domain.intents import AlgoParams, Intent


class Algorithm(ABC):
    """Base class for a trading strategy.

    A strategy decides *what* should happen and says so by yielding intents.
    It never places an order, writes to the DB, or posts to Discord — that is
    `bot/runner.py`'s job. Anything the strategy needs to remember between
    polls (seen trade ids, caches) lives on the subclass.
    """

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
