"""
Phase 2+4: Trade executor with position tracking and risk management.
"""

import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from . import config, fetcher, notifier
from .models import Trade
from .positions import PositionTracker, RiskManager

logger = logging.getLogger(__name__)


def build_client() -> Optional[ClobClient]:
    if not config.POLY_PRIVATE_KEY:
        logger.warning("POLY_PRIVATE_KEY not set — running in paper-trade mode only.")
        return None

    creds = None
    if config.POLY_API_KEY:
        creds = ApiCreds(
            api_key=config.POLY_API_KEY,
            api_secret=config.POLY_API_SECRET,
            api_passphrase=config.POLY_API_PASSPHRASE,
        )

    return ClobClient(
        host=config.CLOB_API,
        key=config.POLY_PRIVATE_KEY,
        chain_id=POLYGON,
        creds=creds,
        signature_type=2,
        funder=config.POLY_FUNDER_ADDRESS or None,
    )


def execute(
    trade: Trade,
    client: Optional[ClobClient],
    tracker: PositionTracker,
    risk: RiskManager,
) -> None:
    """
    Copy a trade from surfandturf.

    BUY  → scale by SCALE_FACTOR, run risk checks, place order, record position.
    SELL → close our full position in that market (mirror close).
    """
    if not trade.asset_id and trade.action != "REDEEM":
        logger.warning("Trade missing asset_id, skipping: %s", trade)
        return

    paper = config.PAPER_TRADE or client is None

    notifier.on_trade_detected(trade)

    if trade.action == "BUY":
        _handle_buy(trade, client, tracker, risk, paper)
    elif trade.action == "SELL":
        _handle_sell(trade, client, tracker, risk, paper)
    elif trade.action == "REDEEM":
        _handle_redeem(trade, tracker, paper)


# ---------------------------------------------------------------------------
# BUY
# ---------------------------------------------------------------------------

def _handle_buy(
    trade: Trade,
    client: Optional[ClobClient],
    tracker: PositionTracker,
    risk: RiskManager,
    paper: bool,
) -> None:
    # Look up surfandturf's total position value in this market to determine tier
    holding = fetcher.fetch_target_position_value(
        config.TARGET_ADDRESS, trade.market_id
    )
    logger.info(
        "Target holding in market: $%.0f | %s", holding, trade.question[:55]
    )

    if holding < config.TIER1_MIN:
        logger.info(
            "Holding $%.0f below tier 1 min $%.0f, skipping: %s",
            holding, config.TIER1_MIN, trade.question[:50],
        )
        return
    elif holding <= config.TIER1_MAX:
        scaled_usdc = config.TIER1_SIZE
    else:
        scaled_usdc = config.TIER2_SIZE  # covers TIER1_MAX–TIER2_MAX and above

    logger.info(
        "Tier bet: $%.2f (target holding $%.0f) | %s",
        scaled_usdc, holding, trade.question[:55],
    )

    approved, reason = risk.check(trade, scaled_usdc)
    if not approved:
        logger.warning("Risk check failed — %s | %s", reason, trade.question[:50])
        notifier.on_risk_blocked(reason, trade)
        return

    if not paper and not _slippage_ok(trade, client):
        return

    if paper:
        logger.info(
            "PAPER BUY | $%.2f (%.0f%% of $%.2f) @ %.3f | %s — %s",
            scaled_usdc, config.SCALE_FACTOR * 100, trade.size_usdc,
            trade.price, trade.outcome, trade.question[:55],
        )
    else:
        _place_buy(trade, scaled_usdc, client)

    notifier.on_buy_executed(trade, scaled_usdc, paper)
    tracker.record_buy(trade, scaled_usdc, paper=paper)
    tracker.print_summary()


# ---------------------------------------------------------------------------
# SELL (mirror close)
# ---------------------------------------------------------------------------

def _handle_sell(
    trade: Trade,
    client: Optional[ClobClient],
    tracker: PositionTracker,
    risk: RiskManager,
    paper: bool,
) -> None:
    position = tracker.get(trade.market_id)

    if position is None or position.shares <= 0:
        logger.info(
            "No position to close for: %s", trade.question[:60]
        )
        return

    approved, reason = risk.check(trade, 0)
    if not approved:
        logger.warning("Risk check blocked sell — %s", reason)
        notifier.on_risk_blocked(reason, trade)
        return

    shares = position.shares
    proceeds_usdc = shares * trade.price
    pnl = (trade.price - position.avg_price) * shares

    if not paper and not _slippage_ok(trade, client):
        return

    if paper:
        logger.info(
            "PAPER SELL | %.2f shares @ %.3f ($%.2f) | P&L $%.2f | %s",
            shares, trade.price, proceeds_usdc, pnl, trade.question[:55],
        )
    else:
        _place_sell(trade, shares, client)

    notifier.on_sell_executed(trade, shares, pnl, paper)
    tracker.record_sell(trade, shares, proceeds_usdc, paper=paper)
    tracker.print_summary()


# ---------------------------------------------------------------------------
# REDEEM (market resolved)
# ---------------------------------------------------------------------------

def _handle_redeem(trade: Trade, tracker: PositionTracker, paper: bool) -> None:
    position = tracker.get(trade.market_id)
    if position is None or position.shares <= 0:
        return

    # Determine resolution price from the CLOB last-trade-price endpoint.
    # Winning token → ~1.0, losing token → ~0.0.
    resolution_price = fetcher.fetch_resolution_price(position.asset_id)

    if resolution_price is None:
        logger.warning(
            "Could not determine resolution for %s — position left open.",
            trade.question[:60],
        )
        return

    if resolution_price > 0.9:
        close_price = 1.0
        result = "WIN"
    elif resolution_price < 0.1:
        close_price = 0.0
        result = "LOSS"
    else:
        logger.warning(
            "Ambiguous resolution price %.3f for %s — position left open.",
            resolution_price, trade.question[:60],
        )
        return

    proceeds = position.shares * close_price
    pnl = (close_price - position.avg_price) * position.shares

    logger.info(
        "%sREDEEM %s | %.2f shares → $%.2f | P&L $%.2f | %s",
        "PAPER " if paper else "", result, position.shares,
        proceeds, pnl, trade.question[:55],
    )

    tracker.record_sell(trade, position.shares, proceeds, paper=paper)
    tracker.print_summary()

    sign = "+" if pnl >= 0 else ""
    notifier.send(
        f"{'📄 PAPER ' if paper else ''}{'✅ WIN' if result == 'WIN' else '❌ LOSS'}\n"
        f"{position.outcome} resolved | {position.shares:.2f} shares "
        f"→ ${proceeds:.2f} | P&L {sign}${pnl:.2f}\n"
        f"{trade.question[:80]}"
    )


# ---------------------------------------------------------------------------
# Slippage check
# ---------------------------------------------------------------------------

def _slippage_ok(trade: Trade, client: ClobClient) -> bool:
    try:
        resp = client.get_last_trade_price(trade.asset_id)
        current_price = float(resp.get("price", trade.price))
    except Exception as e:
        logger.warning("Could not fetch current price: %s", e)
        return True

    if trade.price == 0:
        return True

    drift = abs(current_price - trade.price) / trade.price
    if drift > config.MAX_SLIPPAGE:
        logger.warning(
            "Slippage %.1f%% > max %.1f%%, skipping: %s",
            drift * 100, config.MAX_SLIPPAGE * 100, trade.question[:50],
        )
        notifier.on_slippage_skipped(trade, drift * 100)
        return False
    return True


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _place_buy(trade: Trade, scaled_usdc: float, client: ClobClient) -> None:
    try:
        if config.ORDER_TYPE == "market":
            args = MarketOrderArgs(
                token_id=trade.asset_id,
                amount=scaled_usdc,
                side="BUY",
                price=trade.price,
            )
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FOK)
        else:
            shares = scaled_usdc / trade.price
            args = OrderArgs(
                token_id=trade.asset_id,
                price=trade.price,
                size=shares,
                side="BUY",
            )
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)
        logger.info("BUY order response: %s", resp)
    except Exception as e:
        logger.error("BUY order failed: %s", e)


def _place_sell(trade: Trade, shares: float, client: ClobClient) -> None:
    try:
        if config.ORDER_TYPE == "market":
            args = MarketOrderArgs(
                token_id=trade.asset_id,
                amount=shares,   # SELL amount is in shares
                side="SELL",
                price=trade.price,
            )
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FOK)
        else:
            args = OrderArgs(
                token_id=trade.asset_id,
                price=trade.price,
                size=shares,
                side="SELL",
            )
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)
        logger.info("SELL order response: %s", resp)
    except Exception as e:
        logger.error("SELL order failed: %s", e)
