"""Portfolio value objects independent of SQLite and risk policy."""

from dataclasses import dataclass


@dataclass
class Position:
    """One algorithm's holding and canonical cost basis in a market."""

    market_id: str
    asset_id: str
    question: str
    outcome: str
    shares: float
    avg_price: float
    total_cost_usdc: float
    opened_at: int
    updated_at: int

    def __str__(self) -> str:
        return (
            f"{self.outcome} | {self.shares:.2f} shares @ avg {self.avg_price:.3f}"
            f" | cost ${self.total_cost_usdc:.2f} | {self.question[:55]}"
        )
