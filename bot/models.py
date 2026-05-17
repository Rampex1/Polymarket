from dataclasses import dataclass
from typing import Optional


@dataclass
class Trade:
    id: str
    market_id: str
    question: str
    side: str          # "YES" or "NO"
    size_usdc: float
    price: float       # probability, 0–1
    action: str        # "BUY" or "SELL"
    timestamp: int     # unix seconds
    outcome: str       # e.g. "Yes", "No"
    asset_id: Optional[str] = None

    def __str__(self) -> str:
        direction = f"{self.action} {self.side}"
        return (
            f"[{self.timestamp}] {direction} ${self.size_usdc:.2f} "
            f"@ {self.price:.3f} | {self.question[:60]}"
        )
