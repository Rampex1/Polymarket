"""External-market records used at integration boundaries."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Trade:
    """A source-wallet activity event or execution context.

    The name is retained for compatibility.  New code should treat this as a
    boundary record, not as the internal command model; commands are Intents.
    """

    id: str
    market_id: str
    question: str
    side: str
    size_usdc: float
    price: float
    action: str
    timestamp: int
    outcome: str
    asset_id: Optional[str] = None

    def __str__(self) -> str:
        return (
            f"[{self.timestamp}] {self.action} {self.side} ${self.size_usdc:.2f} "
            f"@ {self.price:.3f} | {self.question[:60]}"
        )


@dataclass
class GlobalTrade:
    """A parsed platform-wide Data API trade-firehose record."""

    tx_hash: str
    wallet: str
    side: str
    price: float
    shares: float
    cash_usdc: float
    market_id: str
    asset_id: str
    timestamp: int
    title: str = ""
    outcome: str = ""
    trader_name: str = ""

    def __str__(self) -> str:
        return (
            f"[{self.timestamp}] {self.wallet[:8]}… {self.side} "
            f"${self.cash_usdc:,.0f} @ {self.price:.3f} | {self.title[:50]}"
        )
