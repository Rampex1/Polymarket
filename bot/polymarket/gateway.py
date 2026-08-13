"""Single read-side gateway for Polymarket data.

``bot.polymarket.api`` is the transport; everything on the trading path —
strategies and execution alike — reads through this object instead, so the
external dependency is explicit in a signature and replaceable in tests.
The archiver and the offline history importer bypass it deliberately: they
are separate jobs that inject their own session and market lookups.
"""

from typing import Optional

from . import api
from ..domain.records import GlobalTrade, Trade


class MarketDataGateway:
    """Read-only access to activity, market metadata, and CLOB prices."""

    def lookup_wallet(self, username: str) -> Optional[str]:
        return api.lookup_wallet(username)

    def recent_trades(self, address: str, limit: int = 100) -> list[Trade]:
        return api.fetch_recent_trades(address, limit)

    def user_positions(self, address: str, limit: int = 500, offset: int = 0) -> list[dict]:
        return api.fetch_user_positions(address, limit, offset)

    def target_position_value(
        self, address: str, market_id: str, expected_min: float = 0.0,
    ) -> Optional[float]:
        return api.fetch_target_position_value(address, market_id, expected_min)

    def global_trades(self, min_cash_usdc: float, limit: int = 100) -> list[GlobalTrade]:
        return api.fetch_global_trades(min_cash_usdc, limit)

    def market(self, market_id: str) -> Optional[dict]:
        return api.fetch_market_resolution(market_id)

    def price(self, asset_id: str) -> Optional[float]:
        return api.fetch_resolution_price(asset_id)

    def wallet_stats(self, address: str, max_rows: int = 100) -> Optional[dict]:
        return api.fetch_wallet_stats(address, max_rows)

    def wallet_value(self, address: str) -> Optional[float]:
        return api.fetch_wallet_value(address)

    @staticmethod
    def market_labels(market: dict) -> str:
        return api.market_labels(market)

    @staticmethod
    def market_end_ts(market: dict) -> Optional[float]:
        return api.market_end_ts(market)

    @staticmethod
    def market_is_resolved(market: dict) -> bool:
        return api.market_is_resolved(market)

    @staticmethod
    def market_outcome_is_final(market: dict) -> bool:
        return api.market_outcome_is_final(market)


DEFAULT_MARKET_DATA = MarketDataGateway()
