"""The stable language between strategies and execution.

Strategies only decide *what* should happen.  They emit these immutable-ish
data objects; execution owns all side effects such as risk checks, orders,
persistence, and notifications.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Protocol


class Mode(str, Enum):
    """Execution mode selected independently for each algorithm."""

    PAPER = "paper"
    LIVE = "live"


@dataclass
class OpenIntent:
    """Open or top up a position by spending ``usdc_amount``."""

    market_id: str
    asset_id: str
    usdc_amount: float
    signal_price: float
    question: str = ""
    outcome: str = ""
    signal_id: str = ""
    reason: str = ""
    features: dict = field(default_factory=dict)
    # Optional leader attribution.  Generic strategies leave these blank;
    # watchlist copy trading uses them to mirror that leader's later exit.
    leader_wallet: str = ""
    leader_event_id: str = ""


@dataclass
class CloseIntent:
    """Close a fraction of the held position in a market."""

    market_id: str
    fraction: float
    signal_price: float
    question: str = ""
    outcome: str = ""
    signal_id: str = ""
    reason: str = ""
    leader_wallet: str = ""
    leader_event_id: str = ""


@dataclass
class SettleIntent:
    """Settle a position using the market's canonical resolution price."""

    market_id: str
    question: str = ""
    outcome: str = ""
    signal_id: str = ""
    reason: str = ""


Intent = OpenIntent | CloseIntent | SettleIntent


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


class IntentSource(Protocol):
    """Minimal strategy protocol, kept separate from worker concerns."""

    params: AlgoParams

    def poll(self) -> Iterator[Intent]: ...
