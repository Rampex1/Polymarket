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


@dataclass
class GlobalTrade:
    """A row from the platform-wide Data-API `/trades` firehose.

    Distinct from `Trade` (the per-wallet /activity shape): firehose rows
    identify the *trader* (`wallet`) and report shares+price rather than a
    pre-computed USDC notional, so `cash_usdc` is derived at parse time.
    """
    tx_hash: str
    wallet: str          # the trader's proxy wallet
    side: str            # "BUY" or "SELL"
    price: float         # probability, 0–1
    shares: float        # token units (the API's `size` field — NOT usd)
    cash_usdc: float     # shares × price
    market_id: str       # conditionId
    asset_id: str        # CLOB token id
    timestamp: int       # unix seconds
    title: str = ""
    outcome: str = ""
    trader_name: str = ""    # display name/pseudonym, informational only

    def __str__(self) -> str:
        return (
            f"[{self.timestamp}] {self.wallet[:8]}… {self.side} "
            f"${self.cash_usdc:,.0f} @ {self.price:.3f} | {self.title[:50]}"
        )
