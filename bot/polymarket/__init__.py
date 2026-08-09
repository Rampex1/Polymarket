"""
polymarket

Everything that talks to Polymarket: raw HTTP reads (api) and the gateway
strategies depend on instead of the transport.
"""

from .gateway import DEFAULT_MARKET_DATA, MarketDataGateway

__all__ = ["DEFAULT_MARKET_DATA", "MarketDataGateway"]
