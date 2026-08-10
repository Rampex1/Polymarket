"""
intents.py

What we decided to do — the stable language between strategies and
execution. A strategy emits these; the runner owns every side effect.
"""

from dataclasses import dataclass, field


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
    # Optional attribution: what caused this fill, as an opaque key. Blank
    # means the position is undivided. Execution records it as a lot so a
    # later close for the same source unwinds only that source's share.
    source: str = ""
    source_event_id: str = ""


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
    source: str = ""
    source_event_id: str = ""


@dataclass
class SettleIntent:
    """Settle a position using the market's canonical resolution price."""

    market_id: str
    question: str = ""
    outcome: str = ""
    signal_id: str = ""
    reason: str = ""


Intent = OpenIntent | CloseIntent | SettleIntent
