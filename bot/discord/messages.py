"""
messages.py

Pure Discord formatting helpers.

Keeping escaping and presentation rules separate from webhook delivery makes
notification text easy to test and reuse from the interactive Discord bot.
"""

import re
import time

_DISCORD_ESCAPE = re.compile(r"([\\*_~`|>])")


def escape(value: str) -> str:
    """Escape Discord markdown in an external/user-controlled value."""
    return _DISCORD_ESCAPE.sub(r"\\\1", value or "")


def subtitle(algo_name: str) -> str:
    return f" · {escape(algo_name)}" if algo_name else ""


def question(value: str) -> str:
    return f"**{escape((value or '')[:80])}**"


def market_url(market_id: str) -> str:
    return f"https://polymarket.com/event/{market_id}" if market_id else ""


def feature_line(features: dict) -> str:
    """Render the optional raw signal observables in a compact form."""
    if not features:
        return ""
    parts = []
    age = features.get("wallet_age_seconds")
    if age is not None:
        parts.append(f"wallet {age / 86_400:.1f}d old")
    trade_count = features.get("trade_count")
    if trade_count is not None:
        parts.append(f"{trade_count} prior trades")
    portfolio = features.get("portfolio_value_usdc")
    if portfolio is not None:
        parts.append(f"portfolio ${portfolio:,.0f}")
    category = features.get("market_category")
    if category:
        parts.append(escape(str(category)))
    end_ts = features.get("market_end_ts")
    if end_ts is not None:
        # Hours under a day: whole days alone render every sub-day horizon as
        # "0d", which is the least useful thing this line can say for a
        # strategy whose markets all resolve within one.
        left = max(0.0, end_ts - time.time())
        parts.append(
            f"resolves in {left / 3_600:.1f}h" if left < 86_400
            else f"resolves in {left / 86_400:.0f}d"
        )
    return " · ".join(parts)
