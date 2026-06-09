"""
Algorithm contract — the abstraction every trading strategy implements.

Each algorithm runs as a fully independent worker (own poll cadence, own
risk pool, own paper bankroll, own positions namespace via the `algo`
column in the DB). All it has to do is yield `Intent`s; the shared runner
handles slippage, order placement, DB writes, and notifications.

Design notes
------------
* Intents carry *enough* fields for the existing tracker/notifier APIs
  (question, outcome) so the runner can construct synthetic Trade objects
  for legacy interfaces without round-tripping through the Polymarket
  activity API.
* `signal_price` of 0 disables the slippage gate — used for forced exits
  (e.g. MERGE close) where we have to take whatever the book offers.
* Subclasses set `params` at class- or instance-level; the runner reads
  algo-specific risk/sizing knobs from there.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Protocol


# ---------------------------------------------------------------------------
# Mode — per-algorithm paper/live toggle
# ---------------------------------------------------------------------------


class Mode(str, Enum):
    """Per-algorithm execution mode.

    Replaces the previous global PAPER_TRADE flag. Each algorithm declares
    its mode in its params, so a single process can run prod-live and
    paper-experimental algorithms side by side without sharing the toggle.
    Inherits from str so env-var values ("paper" / "live") work directly.
    """
    PAPER = "paper"
    LIVE = "live"


# ---------------------------------------------------------------------------
# Intents — the language an algorithm speaks to the runner
# ---------------------------------------------------------------------------


@dataclass
class OpenIntent:
    """Open or top up a position by spending `usdc_amount`."""
    market_id: str
    asset_id: str
    usdc_amount: float
    signal_price: float          # for slippage gate; must be > 0
    question: str = ""
    outcome: str = ""
    signal_id: str = ""          # tx hash / arbitrary unique trigger id
    reason: str = ""             # human-readable, surfaced to logs/Telegram


@dataclass
class CloseIntent:
    """Close a fraction (0..1) of our position in this market."""
    market_id: str
    fraction: float
    signal_price: float          # 0.0 disables the slippage gate
    question: str = ""
    outcome: str = ""
    signal_id: str = ""
    reason: str = ""


@dataclass
class SettleIntent:
    """Resolved market — settle our position at the canonical close price."""
    market_id: str
    question: str = ""
    outcome: str = ""
    signal_id: str = ""
    reason: str = ""


Intent = OpenIntent | CloseIntent | SettleIntent


# ---------------------------------------------------------------------------
# Params Protocol — the knobs the shared runner + RiskManager read
# ---------------------------------------------------------------------------


class AlgoParams(Protocol):
    """Structural type the runner + RiskManager rely on.

    Concrete params classes (one per algorithm) just need to expose these
    attributes; subclassing is optional.
    """
    name: str
    mode: Mode

    # Risk knobs
    max_position_size_usdc: float
    max_total_exposure_usdc: float
    daily_loss_limit_usdc: float
    min_order_size_usdc: float
    max_slippage: float

    # Polling
    poll_interval_seconds: int

    # Paper mode
    paper_starting_balance: float

    # Order placement
    order_type: str              # "market" or "limit"
    paper_fee_bps: float


# ---------------------------------------------------------------------------
# Algorithm base class
# ---------------------------------------------------------------------------


class Algorithm(ABC):
    """A trading strategy. Stateful, one instance per running worker.

    Subclass contract
    -----------------
    * Set `params` (class or instance attribute) to an `AlgoParams`-shaped object.
    * Override `setup()` if you need to resolve external state (look up
      target wallet, load a watchlist, etc.) — called once at startup.
    * Override `poll()` to yield zero or more `Intent`s each tick.
    """

    params: AlgoParams

    def setup(self, tracker, notifier_mod, client) -> None:
        """Called once before the poll loop starts.

        `tracker` and `risk` are already scoped to this algorithm. The
        default implementation is a no-op — override if you need to
        warm caches or resolve config.
        """

    @abstractmethod
    def poll(self) -> Iterator[Intent]:
        """Called every tick. Yield zero or more Intents to execute."""

    @property
    def name(self) -> str:
        """Convenience — most code reads `algo.params.name`."""
        return self.params.name
