"""
Trade executor — dispatches BUY / SELL / REDEEM signals from the target.

Hard rules enforced here (these were bugs before):

  * Order placement returns a `FillResult` with the *actual* shares and
    fill price. The DB is updated *only* on a successful fill — a rejected
    or partial FOK no longer leaves a phantom position behind.

  * Slippage and fees are modeled in paper mode too. Paper that ignores
    slippage and fees overstates profitability vs. live by enough to
    invalidate any decision based on its P&L.

  * The mirror SELL is *proportional* to the target's sell ratio, not a
    blanket full-close. Mirrors target conviction (a 10% trim is not a full
    exit).
"""

import logging
from dataclasses import dataclass
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from . import config, fetcher, notifier
from .models import Trade
from .positions import PositionTracker, RiskManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FillResult — what `_place_*` returns to the caller
# ---------------------------------------------------------------------------

@dataclass
class FillResult:
    """Outcome of an order placement.

    `success=False` means nothing was filled — caller MUST NOT record a
    position. For partial fills `success=True` and `shares`/`spent_usdc`
    reflect what actually filled.
    """
    success: bool
    shares: float
    spent_usdc: float       # for BUY: amount spent; for SELL: proceeds received
    fill_price: float
    fee_usdc: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def execute(
    trade: Trade,
    client: Optional[ClobClient],
    tracker: PositionTracker,
    risk: RiskManager,
) -> None:
    """Copy a target trade.

    BUY    → tier-size, run risk checks, place order, record position.
    SELL   → mirror-close *proportionally* to the target's sell ratio.
    REDEEM → settle our matching position at the resolution price.
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
    # Look up the target's total position value in this market to pick a tier.
    # We pass `expected_min=trade.size_usdc` because the trade we just observed
    # *must* be reflected in the holding; if the API hasn't caught up, retry.
    holding = fetcher.fetch_target_position_value(
        config.TARGET_ADDRESS, trade.market_id, expected_min=trade.size_usdc,
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

    scaled_usdc = _tier_for_holding(holding)
    logger.info(
        "Tier bet: $%.2f (target holding $%.0f) | %s",
        scaled_usdc, holding, trade.question[:55],
    )

    approved, reason = risk.check(trade, scaled_usdc, paper)
    if not approved:
        logger.warning("Risk check failed — %s | %s", reason, trade.question[:50])
        notifier.on_risk_blocked(reason, trade)
        return

    # Slippage check runs in BOTH paper and live so paper accounting reflects
    # what live would actually do. Get the current mid for fill simulation too.
    current_price = _get_current_price(trade, client)
    if not _slippage_ok(trade, current_price):
        return

    # Place (or simulate) the order. fill is None on failure.
    if paper:
        fill = _simulate_buy(trade, scaled_usdc, current_price)
    else:
        fill = _place_buy(trade, scaled_usdc, client)

    if not fill or not fill.success:
        reason_str = fill.reason if fill else "no fill"
        logger.warning("BUY did not fill (%s) — not recording position.", reason_str)
        notifier.on_buy_failed(trade, reason_str)
        return

    notifier.on_buy_executed(trade, fill.spent_usdc, paper, fill.fill_price)
    tracker.record_buy(
        trade,
        spent_usdc=fill.spent_usdc,
        shares=fill.shares,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    tracker.print_summary(paper=paper)


def _tier_for_holding(holding: float) -> float:
    """Map a USD holding to a tier bet size. Pure function for easy testing."""
    if holding <= config.TIER1_MAX:
        return config.TIER1_SIZE
    if holding <= config.TIER2_MAX:
        return config.TIER2_SIZE
    return config.TIER3_SIZE


# ---------------------------------------------------------------------------
# SELL (proportional mirror close)
# ---------------------------------------------------------------------------

def _handle_sell(
    trade: Trade,
    client: Optional[ClobClient],
    tracker: PositionTracker,
    risk: RiskManager,
    paper: bool,
) -> None:
    position = tracker.get(trade.market_id, paper)

    if position is None or position.shares <= 0:
        logger.info("No position to close for: %s", trade.question[:60])
        return

    # Risk checks for sells are effectively no-ops (see RiskManager.check)
    # because a sell *reduces* risk. We still call it so any future per-sell
    # gating (e.g. "halt during a market freeze") has a single place to live.
    approved, reason = risk.check(trade, 0, paper)
    if not approved:
        logger.warning("Risk check blocked sell — %s", reason)
        notifier.on_risk_blocked(reason, trade)
        return

    # Proportional mirror: scale our close by the same fraction the target
    # is closing. We need their prior-trade holding to compute the ratio.
    target_holding_after = fetcher.fetch_target_position_value(
        config.TARGET_ADDRESS, trade.market_id,
    )
    target_holding_before = target_holding_after + trade.size_usdc

    if target_holding_before <= 0:
        sell_ratio = 1.0   # fallback: full close
    else:
        sell_ratio = max(0.0, min(1.0, trade.size_usdc / target_holding_before))

    shares_to_sell = position.shares * sell_ratio
    if shares_to_sell < 1e-6:
        logger.info("Computed sell ratio %.4f → no-op", sell_ratio)
        return

    logger.info(
        "Mirror close: %.1f%% of position (%.2f / %.2f shares)",
        sell_ratio * 100, shares_to_sell, position.shares,
    )

    current_price = _get_current_price(trade, client)
    if not _slippage_ok(trade, current_price):
        return

    if paper:
        fill = _simulate_sell(trade, shares_to_sell, current_price)
    else:
        fill = _place_sell(trade, shares_to_sell, client)

    if not fill or not fill.success:
        reason_str = fill.reason if fill else "no fill"
        logger.warning("SELL did not fill (%s) — position unchanged.", reason_str)
        return

    # Realized P&L is computed inside record_sell against our stored avg_price.
    pnl = (fill.spent_usdc - fill.fee_usdc) - (position.avg_price * fill.shares)
    notifier.on_sell_executed(trade, fill.shares, pnl, paper, fill.fill_price)
    tracker.record_sell(
        trade,
        shares=fill.shares,
        proceeds_usdc=fill.spent_usdc,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    tracker.print_summary(paper=paper)


# ---------------------------------------------------------------------------
# REDEEM (market resolved)
# ---------------------------------------------------------------------------

def _handle_redeem(trade: Trade, tracker: PositionTracker, paper: bool) -> None:
    position = tracker.get(trade.market_id, paper)
    if position is None or position.shares <= 0:
        return

    # Prefer Gamma's canonical resolution when available; fall back to the
    # CLOB last-trade-price heuristic. This avoids the previous bug where a
    # stale mid-price between 0.1 and 0.9 left positions open forever.
    close_price = _resolve_close_price(trade.market_id, position.asset_id)
    if close_price is None:
        logger.warning(
            "Could not determine resolution for %s — position left open.",
            trade.question[:60],
        )
        return

    result = "WIN" if close_price > 0.5 else "LOSS"
    proceeds = position.shares * close_price
    pnl = (close_price - position.avg_price) * position.shares

    logger.info(
        "%sREDEEM %s | %.2f shares → $%.2f | P&L $%.2f | %s",
        "PAPER " if paper else "", result, position.shares,
        proceeds, pnl, trade.question[:55],
    )

    tracker.record_sell(
        trade,
        shares=position.shares,
        proceeds_usdc=proceeds,
        fill_price=close_price,
        paper=paper,
        fee_usdc=0.0,   # redemptions don't pay trade fees
    )
    tracker.print_summary(paper=paper)

    sign = "+" if pnl >= 0 else ""
    notifier.send(
        f"{'📄 PAPER ' if paper else ''}{'✅ WIN' if result == 'WIN' else '❌ LOSS'}\n"
        f"{position.outcome} resolved | {position.shares:.2f} shares "
        f"→ ${proceeds:.2f} | P&L {sign}${pnl:.2f}\n"
        f"{trade.question[:80]}"
    )


def _resolve_close_price(market_id: str, asset_id: str) -> Optional[float]:
    """Determine the final price for a redeemed position.

    Strategy:
      1. Ask Gamma for the market's resolution and find which outcome won.
         If our asset_id matches the winning token, price = 1.0; else 0.0.
      2. Fall back to the CLOB last-trade-price (~1.0 = win, ~0.0 = loss).
    """
    market = fetcher.fetch_market_resolution(market_id)
    if market:
        # Resolved markets expose either a `outcomePrices` JSON string
        # ("[1.0, 0.0]") or per-outcome token info — try to match by token.
        tokens = market.get("clobTokenIds")
        prices = market.get("outcomePrices")
        if tokens and prices:
            try:
                import json
                token_list = json.loads(tokens) if isinstance(tokens, str) else tokens
                price_list = json.loads(prices) if isinstance(prices, str) else prices
                for tok, pr in zip(token_list, price_list):
                    if tok == asset_id:
                        return float(pr)
            except (ValueError, TypeError) as e:
                logger.debug("Gamma resolution parse failed: %s", e)

    # Fallback: CLOB last-trade-price (binarized).
    price = fetcher.fetch_resolution_price(asset_id)
    if price is None:
        return None
    if price > 0.9:
        return 1.0
    if price < 0.1:
        return 0.0
    return None


# ---------------------------------------------------------------------------
# Slippage + price lookup
# ---------------------------------------------------------------------------

def _get_current_price(trade: Trade, client: Optional[ClobClient]) -> float:
    """Best-effort current market price for this asset.

    Uses the authenticated client when available (lower latency); falls back
    to the public CLOB endpoint. Returns `trade.price` if everything fails
    so we don't accidentally treat a price-fetch outage as 100% drift.
    """
    if client is not None:
        try:
            resp = client.get_last_trade_price(trade.asset_id)
            return float(resp.get("price", trade.price))
        except Exception as e:
            logger.warning("Authenticated price fetch failed: %s", e)

    price = fetcher.fetch_resolution_price(trade.asset_id)
    return price if price is not None else trade.price


def _slippage_ok(trade: Trade, current_price: float) -> bool:
    if trade.price <= 0:
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
# Paper-mode fill simulation
# ---------------------------------------------------------------------------

def _simulate_buy(trade: Trade, scaled_usdc: float, current_price: float) -> FillResult:
    """Simulate a market BUY at the current price, charging the modeled fee.

    Without this, paper P&L systematically overstates live P&L because every
    paper buy fills at the target's `trade.price` regardless of how the
    market has moved.
    """
    fill_price = current_price if current_price > 0 else trade.price
    if fill_price <= 0:
        return FillResult(False, 0, 0, 0, reason="zero fill price")
    fee = scaled_usdc * (config.PAPER_FEE_BPS / 10_000.0)
    shares = (scaled_usdc - fee) / fill_price
    return FillResult(
        success=True,
        shares=shares,
        spent_usdc=scaled_usdc,
        fill_price=fill_price,
        fee_usdc=fee,
    )


def _simulate_sell(trade: Trade, shares: float, current_price: float) -> FillResult:
    fill_price = current_price if current_price > 0 else trade.price
    if fill_price <= 0:
        return FillResult(False, 0, 0, 0, reason="zero fill price")
    gross = shares * fill_price
    fee = gross * (config.PAPER_FEE_BPS / 10_000.0)
    return FillResult(
        success=True,
        shares=shares,
        spent_usdc=gross,
        fill_price=fill_price,
        fee_usdc=fee,
    )


# ---------------------------------------------------------------------------
# Live order placement — returns FillResult, never silently swallows failure
# ---------------------------------------------------------------------------

def _place_buy(trade: Trade, scaled_usdc: float, client: ClobClient) -> FillResult:
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
        return _parse_fill(resp, scaled_usdc, trade.price, side="BUY")
    except Exception as e:
        logger.error("BUY order failed: %s", e)
        return FillResult(False, 0, 0, 0, reason=str(e))


def _place_sell(trade: Trade, shares: float, client: ClobClient) -> FillResult:
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
        # For sells, the "spent" is actually the proceeds — same plumbing.
        return _parse_fill(resp, shares * trade.price, trade.price, side="SELL")
    except Exception as e:
        logger.error("SELL order failed: %s", e)
        return FillResult(False, 0, 0, 0, reason=str(e))


def _parse_fill(
    resp: dict,
    requested_usdc: float,
    signal_price: float,
    side: str,
) -> FillResult:
    """Pull the actual fill out of a CLOB order response.

    The CLOB returns fields like `success`, `status`, `takingAmount`, and
    `makingAmount`. We accept either name and fall back to the requested
    values so a malformed response still produces a sensible record — but
    a `success=False` status is honored as a hard fail.
    """
    if not isinstance(resp, dict):
        return FillResult(False, 0, 0, 0, reason="non-dict response")

    success = resp.get("success", True)
    status = (resp.get("status") or "").lower()
    if success is False or status in ("rejected", "unmatched", "expired"):
        return FillResult(
            False, 0, 0, 0,
            reason=f"status={status or 'rejected'} | {resp.get('errorMsg', '')}",
        )

    # Polymarket reports filled amounts as `takingAmount` / `makingAmount`.
    # For a market BUY, takingAmount = USDC spent and makingAmount = shares.
    # For a market SELL, makingAmount = USDC received and takingAmount = shares.
    try:
        taking = float(resp.get("takingAmount") or 0)
        making = float(resp.get("makingAmount") or 0)
    except (TypeError, ValueError):
        taking = making = 0.0

    if side == "BUY":
        spent = taking or requested_usdc
        shares = making or (spent / signal_price if signal_price > 0 else 0)
    else:
        spent = making or requested_usdc          # proceeds
        shares = taking or (requested_usdc / signal_price if signal_price > 0 else 0)

    fill_price = (spent / shares) if shares > 0 else signal_price
    if shares <= 0 or fill_price <= 0:
        return FillResult(False, 0, 0, 0, reason="zero-fill response")

    return FillResult(
        success=True,
        shares=shares,
        spent_usdc=spent,
        fill_price=fill_price,
        fee_usdc=float(resp.get("feeRateBps") or 0) * spent / 10_000.0,
    )
