"""Compatibility import path for external-market records.

Canonical definitions now live in :mod:`bot.domain.models`.
"""

from .domain.models import GlobalTrade, Trade

__all__ = ["GlobalTrade", "Trade"]
