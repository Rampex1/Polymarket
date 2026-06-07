"""
Trade executor — dispatches BUY / SELL / REDEEM signals from the target.

Hard rules enforced here (these were bugs before):

  * Order placement returns a `FillResult` with the *actual* shares and
    fill price. The DB is updated *only* on a successful fill — a rejected
    or partial FOK no longer leaves a phantom position behind.

  * Slippage and fees are modeled in paper mode too. Paper that ignores
    slippage and fees overstates profitability vs. live by enough to
    invalidate any decision based on its P&L.

  * The mirror SELL is *proportional* to the target's sell ratio, computed
    using a cached pre-trade holding (populated on BUY). Cache miss falls
    back to a full close — the safe choice when in doubt.

  * Slippage check fails *closed* when current price is unavailable. The
    previous version silently filled at the signal price when CLOB was
    unreachable, defeating slippage protection exactly when it mattered.

  * REDEEM resolution requires the market to actually be closed/resolved
    on Gamma. Live mids in the 0.1–0.9 range no longer book P&L.
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
# FillResult — what `_place_*` and `_simulate_*` return to the caller
# ---------------------------------------------------------------------------

@dataclass
class FillResult:
    """Outcome of an order placement.

    `success=False` means nothing filled — caller MUST NOT record a position.
    For partial fills `success=True` and `shares`/`amount_usdc` reflect what
    actually filled.

    `amount_usdc` is dual-use by side:
      * BUY  → USDC spent acquiring shares
      * SELL → USDC proceeds received from closing shares
    """
    success: bool
    shares: float
    amount_usdc: float
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
    SELL   → mirror-close *proportionally* using the cached pre-sell holding.
    REDEEM → settle our matching position at the actual resolution price.
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
    # Look up the target's total position value to pick a tier.
    # `expected_min=trade.size_usdc` defeats the Data API's eventual
    # consistency: the trade we observed must be reflected.
    holding = fetcher.fetch_target_position_value(
        config.TARGET_ADDRESS, trade.market_id, expected_min=trade.size_usdc,
    )
    # Cache the freshly-observed holding so the SELL path can compute an
    # accurate close ratio without trying to reconstruct it from the API.
    fetcher.target_holding_cache.set(trade.market_id, holding)

    logger.info(
        "Target holding in market: $%.0f | %s", holding, trade.question[:55]
    )

    if holding < config.TIER1_MIN:
        logger.info(
            "Holding $%.0f below tier 1 min $%.0f, skipping: %s",
            holding, config.TIER1_MIN, trade.question[:50],
        )
        return

    target = _tier_for_holding(holding)
    position = tracker.get(trade.market_id, paper)
    current = position.total_cost_usdc if position else 0.0
    scaled_usdc = round(target - current, 8)

    if scaled_usdc <= config.MIN_ORDER_SIZE_USDC:
        logger.info(
            "Already at tier target $%.2f (current $%.2f), skipping: %s",
            target, current, trade.question[:50],
        )
        return

    logger.info(
        "Tier top-up: $%.2f (target $%.2f, current $%.2f, holding $%.0f) | %s",
        scaled_usdc, target, current, holding, trade.question[:55],
    )

    approved, reason = risk.check(trade, scaled_usdc, paper)
    if not approved:
        logger.warning("Risk check failed — %s | %s", reason, trade.question[:50])
        notifier.on_risk_blocked(reason, trade)
        return

    # Slippage check runs in BOTH paper and live so paper accounting
    # reflects what live would actually do.
    current_price = _get_current_price(trade, client)
    if not _slippage_ok(trade, current_price):
        return

    if paper:
        fill = _simulate_buy(trade, scaled_usdc, current_price)
    else:
        fill = _place_buy(trade, scaled_usdc, client)

    if not fill or not fill.success:
        reason_str = fill.reason if fill else "no fill"
        logger.warning("BUY did not fill (%s) — not recording position.", reason_str)
        notifier.on_buy_failed(trade, reason_str)
        return

    # Record BEFORE notifying so a Telegram alert never claims a position
    # exists if the DB write somehow fails (the record_buy guard fires).
    tracker.record_buy(
        trade,
        spent_usdc=fill.amount_usdc,
        shares=fill.shares,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    notifier.on_buy_executed(trade, fill.amount_usdc, paper, fill.fill_price)
    tracker.print_summary(paper=paper)


def _tier_for_holding(holding: float) -> float:
    """Map a USD holding to a tier bet size. Pure function for easy testing."""
    if holding <= config.TIER1_MAX:
        return config.TIER1_SIZE
    if holding <= config.TIER2_MAX:
        return config.TIER2_SIZE
    return config.TIER3_SIZE


# ---------------------------------------------------------------------------
# SELL (proportional mirror close, cache-driven)
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

    approved, reason = risk.check(trade, 0, paper)
    if not approved:
        logger.warning("Risk check blocked sell — %s", reason)
        notifier.on_risk_blocked(reason, trade)
        return

    sell_ratio = _compute_sell_ratio(trade)

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

    pnl = (fill.amount_usdc - fill.fee_usdc) - (position.avg_price * fill.shares)
    tracker.record_sell(
        trade,
        shares=fill.shares,
        proceeds_usdc=fill.amount_usdc,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    notifier.on_sell_executed(trade, fill.shares, pnl, paper, fill.fill_price)
    tracker.print_summary(paper=paper)


def _compute_sell_ratio(trade: Trade) -> float:
    """Determine what fraction of *our* position to close, mirroring the target.

    Strategy:
      * Look up the cached pre-sell holding (populated on every BUY signal).
      * If present: ratio = sell_notional / cached_pre_value. Decrement the
        cache so subsequent partial sells in the same market still compute
        correctly. Clamp to [0, 1].
      * If absent (e.g. first signal we've seen for this market, or cache
        cleared on restart): full close. This is the safe default — we
        de-risk completely rather than guess a partial.

    We deliberately do NOT reconstruct the pre-sell value as
    `holding_after + trade.size_usdc`. Holding values are mark-to-market at
    the current price; trade notional is at the fill price; they don't add
    cleanly. And the API is eventually-consistent so `holding_after` may
    actually still be the pre-sell value.
    """
    cached_pre = fetcher.target_holding_cache.get(trade.market_id)
    if cached_pre is None or cached_pre <= 0:
        logger.info(
            "No cached holding for %s — falling back to full close.",
            trade.market_id[:12],
        )
        return 1.0

    ratio = trade.size_usdc / cached_pre
    # Update the cache to reflect the post-sell holding for subsequent partials.
    fetcher.target_holding_cache.decrement(trade.market_id, trade.size_usdc)
    return max(0.0, min(1.0, ratio))


# ---------------------------------------------------------------------------
# REDEEM (market resolved)
# ---------------------------------------------------------------------------

def _handle_redeem(trade: Trade, tracker: PositionTracker, paper: bool) -> None:
    position = tracker.get(trade.market_id, paper)
    if position is None or position.shares <= 0:
        return

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
        fee_usdc=0.0,
    )
    tracker.print_summary(paper=paper)
    # Cache is no longer meaningful for a settled market — clear it.
    fetcher.target_holding_cache.set(trade.market_id, 0.0)

    sign = "+" if pnl >= 0 else ""
    notifier.send(
        f"{'📄 PAPER ' if paper else ''}{'✅ WIN' if result == 'WIN' else '❌ LOSS'}\n"
        f"{position.outcome} resolved | {position.shares:.2f} shares "
        f"→ ${proceeds:.2f} | P&L {sign}${pnl:.2f}\n"
        f"{trade.question[:80]}"
    )


def _resolve_close_price(market_id: str, asset_id: str) -> Optional[float]:
    """Determine the final price for a redeemed position.

    Strategy (in order):
      1. Ask Gamma for the market and *only* trust outcomePrices if the
         market is explicitly closed/resolved. A live market exposes the
         same field, but it's the *current mid*, not the settlement value.
      2. Fall back to CLOB last-trade-price binarized to {0, 1}.
      3. If neither yields a confident answer, return None and leave the
         position open (caller logs a warning).
    """
    market = fetcher.fetch_market_resolution(market_id)
    if market and _market_is_resolved(market):
        outcome_price = _gamma_outcome_price(market, asset_id)
        if outcome_price is not None:
            return outcome_price

    # Fallback: CLOB last-trade-price binarized. Settled markets trade at ~0
    # or ~1 once resolved; ambiguous mids should refuse to book P&L.
    price = fetcher.fetch_resolution_price(asset_id)
    if price is None:
        return None
    if price > 0.95:
        return 1.0
    if price < 0.05:
        return 0.0
    return None


def _market_is_resolved(market: dict) -> bool:
    """Check Gamma's closed/resolved flags. Either being truthy implies finality."""
    for key in ("closed", "resolved", "archived"):
        val = market.get(key)
        if isinstance(val, bool) and val:
            return True
        if isinstance(val, str) and val.lower() in ("true", "1", "yes"):
            return True
    return False


def _gamma_outcome_price(market: dict, asset_id: str) -> Optional[float]:
    """Extract the settlement price for a specific token from a resolved market."""
    tokens = market.get("clobTokenIds")
    prices = market.get("outcomePrices")
    if not (tokens and prices):
        return None
    try:
        import json
        token_list = json.loads(tokens) if isinstance(tokens, str) else tokens
        price_list = json.loads(prices) if isinstance(prices, str) else prices
        for tok, pr in zip(token_list, price_list):
            if tok == asset_id:
                return float(pr)
    except (ValueError, TypeError) as e:
        logger.debug("Gamma resolution parse failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Slippage + price lookup
# ---------------------------------------------------------------------------

def _get_current_price(trade: Trade, client: Optional[ClobClient]) -> Optional[float]:
    """Best-effort current market price for this asset.

    Returns None on failure so the slippage check can refuse the trade.
    This is the safer choice: an outage that returns trade.price would
    silently disable slippage protection, which is when it matters most.
    """
    if client is not None:
        try:
            resp = client.get_last_trade_price(trade.asset_id)
            return float(resp.get("price"))
        except Exception as e:
            logger.warning("Authenticated price fetch failed: %s", e)

    price = fetcher.fetch_resolution_price(trade.asset_id)
    return price


def _slippage_ok(trade: Trade, current_price: Optional[float]) -> bool:
    """Fail closed when price data is unavailable."""
    if current_price is None:
        logger.warning(
            "No current price for slippage check — refusing trade: %s",
            trade.question[:50],
        )
        notifier.on_slippage_skipped(trade, drift_pct=float("nan"))
        return False
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

def _simulate_buy(trade: Trade, scaled_usdc: float, current_price: Optional[float]) -> FillResult:
    """Simulate a market BUY at the current price, charging the modeled fee.

    Without this, paper P&L systematically overstates live P&L because every
    paper buy would fill at the target's signal price regardless of drift.
    """
    if current_price is None or current_price <= 0:
        return FillResult(False, 0, 0, 0, reason="no current price")
    fee = scaled_usdc * (config.PAPER_FEE_BPS / 10_000.0)
    if scaled_usdc - fee <= 0:
        return FillResult(False, 0, 0, 0, reason="fee exceeds order size")
    shares = (scaled_usdc - fee) / current_price
    return FillResult(
        success=True,
        shares=shares,
        amount_usdc=scaled_usdc,
        fill_price=current_price,
        fee_usdc=fee,
    )


def _simulate_sell(trade: Trade, shares: float, current_price: Optional[float]) -> FillResult:
    if current_price is None or current_price <= 0:
        return FillResult(False, 0, 0, 0, reason="no current price")
    gross = shares * current_price
    fee = gross * (config.PAPER_FEE_BPS / 10_000.0)
    return FillResult(
        success=True,
        shares=shares,
        amount_usdc=gross,             # proceeds (gross of fee)
        fill_price=current_price,
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
        return _parse_fill(resp, side="BUY")
    except Exception as e:
        logger.error("BUY order failed: %s", e)
        return FillResult(False, 0, 0, 0, reason=str(e))


def _place_sell(trade: Trade, shares: float, client: ClobClient) -> FillResult:
    try:
        if config.ORDER_TYPE == "market":
            args = MarketOrderArgs(
                token_id=trade.asset_id,
                amount=shares,
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
        return _parse_fill(resp, side="SELL")
    except Exception as e:
        logger.error("SELL order failed: %s", e)
        return FillResult(False, 0, 0, 0, reason=str(e))


def _parse_fill(resp, side: str) -> FillResult:
    """Pull the actual fill out of a CLOB order response.

    Conservative posture: a success=True response with no parseable fill
    data is treated as **failure**, not silently trusted to match the
    request. This avoids the "phantom position from an async-accepted
    order" bug.

    The field names assumed here (takingAmount / makingAmount) are
    documented for the 0x-style relayer; py_clob_client may surface them
    under different names. This function and its assumptions need to be
    validated against a real captured CLOB response — see TODO.
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

    # Try multiple field-name conventions before giving up.
    taking_raw = resp.get("takingAmount") or resp.get("taker_amount") or resp.get("size_matched")
    making_raw = resp.get("makingAmount") or resp.get("maker_amount") or resp.get("filled_amount")
    try:
        taking = float(taking_raw) if taking_raw is not None else 0.0
        making = float(making_raw) if making_raw is not None else 0.0
    except (TypeError, ValueError):
        return FillResult(False, 0, 0, 0, reason="unparseable amounts")

    if taking <= 0 and making <= 0:
        # success=True but no fill data → treat as failure rather than
        # silently book a phantom position at request-derived numbers.
        return FillResult(
            False, 0, 0, 0,
            reason="success but no fill amounts in response",
        )

    if side == "BUY":
        spent = taking
        shares = making
    else:
        spent = making
        shares = taking

    if shares <= 0 or spent <= 0:
        return FillResult(False, 0, 0, 0, reason="zero fill")

    fill_price = spent / shares
    fee = 0.0
    fee_raw = resp.get("feeRateBps") or resp.get("fee_rate_bps")
    if fee_raw:
        try:
            fee = float(fee_raw) * spent / 10_000.0
        except (TypeError, ValueError):
            pass

    return FillResult(
        success=True,
        shares=shares,
        amount_usdc=spent,
        fill_price=fill_price,
        fee_usdc=fee,
    )
