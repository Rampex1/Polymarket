"""
alerts.py

One message per trade event — signal, buy, sell, settle, risk block, plus
worker lifecycle. Each algorithm supplies its own webhook_url; messages for
one market nest into that market's thread.
"""

import logging

from . import threads
from ..domain.records import Trade
from .messages import (
    escape as _esc,
    feature_line as _feature_line,
    market_url as _market_url,
    question as _q,
    subtitle as _subtitle,
)
from .webhook import send

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Each helper accepts an optional `algo_name` so the message can be
# disambiguated when multiple algorithms run side-by-side.
# ---------------------------------------------------------------------------


def _post(text: str, market_id: str, algo_name: str, webhook_url: str,
          paper: bool) -> None:
    """Append the market link and route into that market's thread."""
    url = _market_url(market_id)
    send(
        text + (f"\n{url}" if url else ""),
        webhook_url=threads.route(webhook_url, market_id, algo_name, paper),
    )


def on_signal(intent, algo_name: str = "", webhook_url: str = "",
              paper: bool = False) -> None:
    """Generic detection notification — intent-based, works for any algorithm."""
    from ..domain.intents import OpenIntent, CloseIntent, SettleIntent
    if isinstance(intent, OpenIntent):
        action_line = (
            f"OPEN `{_esc(intent.outcome) or '—'}`  "
            f"${intent.usdc_amount:,.2f} @ **{intent.signal_price:.3f}**"
        )
    elif isinstance(intent, CloseIntent):
        action_line = (
            f"CLOSE `{_esc(intent.outcome) or 'position'}`  "
            f"{intent.fraction * 100:.0f}% @ **{intent.signal_price:.3f}**"
        )
    elif isinstance(intent, SettleIntent):
        action_line = f"SETTLE `{_esc(intent.outcome) or 'position'}`"
    else:
        action_line = "Unknown intent"

    lines = [
        f"📥 **SIGNAL**{_subtitle(algo_name)}",
        _q(intent.question),
        action_line,
    ]
    reason = getattr(intent, "reason", "")
    if reason:
        lines.append(f"↳ {_esc(reason)}")
    feature_summary = _feature_line(getattr(intent, "features", None) or {})
    if feature_summary:
        lines.append(f"↳ {feature_summary}")
    market_id = getattr(intent, "market_id", "") or ""
    _post("\n".join(lines), market_id, algo_name, webhook_url, paper)


def on_trade_detected(trade: Trade, algo_name: str = "", webhook_url: str = "",
                      paper: bool = False) -> None:
    """Legacy entry point — kept for the wallet-watching CopyTrade flow that
    classifies a Trade before issuing an Intent. New algorithms should call
    `on_signal` instead."""
    _post(
        f"📥 **SIGNAL**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(trade.action)} `{_esc(trade.outcome)}`  "
        f"${trade.size_usdc:,.0f} @ **{trade.price:.3f}**",
        trade.market_id, algo_name, webhook_url, paper,
    )


def on_buy_executed(
    trade: Trade, spent_usdc: float, paper: bool, fill_price: float,
    algo_name: str = "", webhook_url: str = "",
) -> None:
    tag = "📄 **PAPER BUY**" if paper else "✅ **BUY**"
    shares = spent_usdc / fill_price if fill_price > 0 else 0.0
    slip = ""
    if trade.price > 0 and fill_price > 0:
        drift_pct = (fill_price - trade.price) / trade.price * 100
        slip = f" _({drift_pct:+.1f}% slip)_"
    url = _market_url(trade.market_id)
    text = (
        f"{tag}{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"`{_esc(trade.outcome)}`  ${spent_usdc:.2f} → {shares:.2f} shares "
        f"@ **{fill_price:.3f}**{slip}"
        + (f"\n{url}" if url else "")
    )
    # The first fill is what opens the market's thread; later top-ups join it.
    threads.send_or_open(
        text, webhook_url, trade.market_id, algo_name, paper,
        thread_name=(trade.question or trade.market_id)[:100],
    )


def on_buy_failed(trade: Trade, reason: str, algo_name: str = "", webhook_url: str = "",
                  paper: bool = False) -> None:
    _post(
        f"❌ **BUY failed**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(reason)}",
        trade.market_id, algo_name, webhook_url, paper,
    )


def on_sell_executed(
    trade: Trade, shares: float, pnl: float, paper: bool, fill_price: float,
    algo_name: str = "", webhook_url: str = "",
) -> None:
    tag = "📄 **PAPER SELL**" if paper else "✅ **SELL**"
    sign = "+" if pnl >= 0 else ""
    _post(
        f"{tag}{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"`{_esc(trade.outcome)}`  {shares:.2f} shares @ **{fill_price:.3f}** · "
        f"P&L **{sign}${pnl:.2f}**",
        trade.market_id, algo_name, webhook_url, paper,
    )


def on_risk_blocked(reason: str, trade: Trade, algo_name: str = "", webhook_url: str = "",
                    paper: bool = False) -> None:
    _post(
        f"⚠️ **Risk block**{_subtitle(algo_name)}\n"
        f"{_q(trade.question)}\n"
        f"{_esc(reason)}",
        trade.market_id, algo_name, webhook_url, paper,
    )


def on_settle_executed(
    market_id: str,
    question: str,
    outcome: str,
    shares: float,
    proceeds: float,
    pnl: float,
    close_price: float,
    paper: bool,
    algo_name: str = "",
    webhook_url: str = "",
) -> None:
    """Settlement notification — routes to the position's thread if one exists."""
    sign = "+" if pnl >= 0 else ""
    result = "WIN" if close_price > 0.5 else "LOSS"
    tag = "📄 **PAPER SETTLE**" if paper else ("✅ **WIN**" if result == "WIN" else "❌ **LOSS**")
    _post(
        f"{tag}{_subtitle(algo_name)}\n"
        f"**{_esc(question[:80])}**\n"
        f"`{_esc(outcome)}` resolved | {shares:.2f} shares "
        f"→ **${proceeds:.2f}** | P&L **{sign}${pnl:.2f}**",
        market_id, algo_name, webhook_url, paper,
    )


def on_startup(mode: str, exposure: float, algo_name: str = "", webhook_url: str = "") -> None:
    send(
        f"🚀 **Bot started**{_subtitle(algo_name)} — {_esc(mode)} mode\n"
        f"Exposure: **${exposure:.2f}**",
        webhook_url=webhook_url,
    )


def on_shutdown(algo_name: str = "", webhook_url: str = "") -> None:
    send(f"🛑 **Bot stopped**{_subtitle(algo_name)}", webhook_url=webhook_url)


def on_worker_unstable(
    algo_name: str,
    error_count: int,
    last_exc: BaseException,
    webhook_url: str = "",
) -> None:
    """Alert when a worker's poll loop has failed repeatedly."""
    send(
        f"🚨 **Worker unstable**{_subtitle(algo_name)}\n"
        f"> {error_count} consecutive poll failures\n"
        f"> {_esc(str(last_exc)[:200])}",
        webhook_url=webhook_url,
    )


