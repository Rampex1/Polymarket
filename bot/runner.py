"""
Shared, stateless dispatch — turns Intents from algorithms into orders.

The runner is the only place that:
  * talks to the CLOB for order placement and price lookups,
  * runs the slippage gate,
  * simulates paper fills,
  * writes positions / trade_log / paper_balance,
  * fires Discord notifications.

Algorithms emit `OpenIntent` / `CloseIntent` / `SettleIntent` and never touch
any of the above. That's the entire reuse story: write a new algorithm in
~20 lines and inherit all the production-hardened execution logic for free.

What stays here (was in executor.py):
  * `build_client` — CLOB client factory.
  * `FillResult` and the `_simulate_*` / `_place_*` / `_parse_fill` helpers.
  * `_get_current_price`, `_slippage_ok`.
  * REDEEM resolution price logic.

What changed:
  * Slippage cap and order type come from `algo.params`, not module config.
  * Risk + tracker are algo-scoped (each thread gets its own instances).
"""

import logging
from dataclasses import dataclass
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.constants import POLYGON

from . import config, fetcher, notifier
from .algorithm import CloseIntent, Intent, OpenIntent, SettleIntent
from .models import Trade
from .positions import PositionTracker, RiskManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FillResult — outcome of an order placement
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
# CLOB client construction
# ---------------------------------------------------------------------------

def build_client() -> Optional[ClobClient]:
    if not config.POLY_PRIVATE_KEY:
        logger.warning("POLY_PRIVATE_KEY not set — running in paper-trade mode only.")
        return None

    # signature_type=2 (Polymarket proxy-wallet flow) requires the funder
    # address explicitly. Without it the client defaults to the EOA — orders
    # sign correctly but USDC is debited from the wrong account, producing
    # silent ghost positions on the user's actual proxy wallet.
    if not config.POLY_FUNDER_ADDRESS:
        logger.error(
            "POLY_FUNDER_ADDRESS is required for live trading (your Polymarket "
            "proxy wallet, visible in the profile URL). Falling back to paper "
            "mode to prevent fund-routing errors."
        )
        return None

    creds = None
    if config.POLY_API_KEY:
        creds = ApiCreds(
            api_key=config.POLY_API_KEY,
            api_secret=config.POLY_API_SECRET,
            api_passphrase=config.POLY_API_PASSPHRASE,
        )
    else:
        # Without API creds we can sign orders but can't authenticate against
        # the CLOB's authenticated endpoints (e.g. get_last_trade_price). Warn
        # so the operator knows slippage checks will fall back to the public
        # /last-trade-price endpoint.
        logger.warning(
            "POLY_API_KEY/SECRET/PASSPHRASE not set — slippage check will use "
            "the public CLOB endpoint instead of authenticated."
        )

    return ClobClient(
        host=config.CLOB_API,
        key=config.POLY_PRIVATE_KEY,
        chain_id=POLYGON,
        creds=creds,
        signature_type=2,
        funder=config.POLY_FUNDER_ADDRESS,
    )


# ---------------------------------------------------------------------------
# Dispatch — the public entry point algorithms route through
# ---------------------------------------------------------------------------

def dispatch(
    intent: Intent,
    algo,                     # bot.algorithm.Algorithm
    tracker: PositionTracker,
    risk: RiskManager,
    client: Optional[ClobClient],
    paper: bool,
) -> None:
    """Execute one intent. Side-effects: order placement + DB write + notify.

    Idempotency note: the caller (algorithm) is expected to dedupe upstream
    signals so the same intent is not emitted twice. The runner does not
    re-check.
    """
    notifier.on_signal(intent, algo.name)

    if isinstance(intent, OpenIntent):
        _handle_open(intent, algo, tracker, risk, client, paper)
    elif isinstance(intent, CloseIntent):
        _handle_close(intent, algo, tracker, client, paper)
    elif isinstance(intent, SettleIntent):
        _handle_settle(intent, algo, tracker, paper)


# ---------------------------------------------------------------------------
# OPEN — risk check, slippage gate, fill, record
# ---------------------------------------------------------------------------

def _handle_open(
    intent: OpenIntent,
    algo,
    tracker: PositionTracker,
    risk: RiskManager,
    client: Optional[ClobClient],
    paper: bool,
) -> None:
    if not intent.asset_id:
        logger.warning("OpenIntent missing asset_id, skipping: %s", intent.reason)
        return

    # Build a synthetic Trade so existing tracker/risk/notifier APIs work
    # unchanged. (Trade is the legacy data class; intents are the new front-end.)
    trade = _intent_to_trade(intent, action="BUY")

    approved, reason = risk.check(trade, intent.usdc_amount, paper)
    if not approved:
        logger.warning("[%s] Risk check failed — %s | %s",
                       algo.name, reason, trade.question[:50])
        notifier.on_risk_blocked(reason, trade, algo.name)
        return

    current_price = _get_current_price(trade, client)
    if not _slippage_ok(trade, current_price, algo.params.max_slippage, algo.name):
        return

    if paper:
        fill = _simulate_buy(intent.usdc_amount, current_price, algo.params.paper_fee_bps)
    else:
        fill = _place_buy(trade, intent.usdc_amount, client, algo.params.order_type)

    if not fill or not fill.success:
        reason_str = fill.reason if fill else "no fill"
        logger.warning("[%s] BUY did not fill (%s) — not recording.",
                       algo.name, reason_str)
        notifier.on_buy_failed(trade, reason_str, algo.name)
        return

    tracker.record_buy(
        trade,
        spent_usdc=fill.amount_usdc,
        shares=fill.shares,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    notifier.on_buy_executed(trade, fill.amount_usdc, paper, fill.fill_price, algo.name)
    tracker.print_summary(paper=paper)


# ---------------------------------------------------------------------------
# CLOSE — fractional sell of our position
# ---------------------------------------------------------------------------

def _handle_close(
    intent: CloseIntent,
    algo,
    tracker: PositionTracker,
    client: Optional[ClobClient],
    paper: bool,
) -> None:
    position = tracker.get(intent.market_id, paper)
    if position is None or position.shares <= 0:
        logger.info("[%s] CLOSE with no position: %s",
                    algo.name, intent.reason or intent.market_id[:12])
        return

    # CLOSE intents carry no asset_id (a market has two sides; we close
    # whichever one we hold). Use the held position's asset_id for the SELL.
    trade = _intent_to_trade(
        intent, action="SELL", asset_id_override=position.asset_id,
        question=intent.question or position.question,
        outcome=intent.outcome or position.outcome,
    )

    shares_to_sell = position.shares * max(0.0, min(1.0, intent.fraction))
    if shares_to_sell < 1e-6:
        logger.info("[%s] Close fraction %.4f → no-op", algo.name, intent.fraction)
        return

    logger.info(
        "[%s] Mirror close: %.1f%% (%.2f / %.2f shares) | %s",
        algo.name, intent.fraction * 100, shares_to_sell, position.shares,
        trade.question[:55],
    )

    current_price = _get_current_price(trade, client)
    # signal_price=0 disables the slippage gate (forced exits like MERGE).
    if intent.signal_price > 0:
        if not _slippage_ok(trade, current_price, algo.params.max_slippage, algo.name):
            return
    else:
        if current_price is None:
            logger.warning("[%s] No current price — cannot close.", algo.name)
            return

    if paper:
        fill = _simulate_sell(shares_to_sell, current_price, algo.params.paper_fee_bps)
    else:
        fill = _place_sell(trade, shares_to_sell, client, algo.params.order_type)

    if not fill or not fill.success:
        reason_str = fill.reason if fill else "no fill"
        logger.warning("[%s] SELL did not fill (%s) — position unchanged.",
                       algo.name, reason_str)
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
    notifier.on_sell_executed(trade, fill.shares, pnl, paper, fill.fill_price, algo.name)
    tracker.print_summary(paper=paper)


# ---------------------------------------------------------------------------
# SETTLE — market resolved, book at canonical close price
# ---------------------------------------------------------------------------

def _handle_settle(
    intent: SettleIntent,
    algo,
    tracker: PositionTracker,
    paper: bool,
) -> None:
    position = tracker.get(intent.market_id, paper)
    if position is None or position.shares <= 0:
        return

    close_price = _resolve_close_price(intent.market_id, position.asset_id)
    if close_price is None:
        logger.warning(
            "[%s] Could not determine resolution for %s — position left open.",
            algo.name, intent.question[:60] or intent.market_id[:12],
        )
        return

    result = "WIN" if close_price > 0.5 else "LOSS"
    proceeds = position.shares * close_price
    pnl = (close_price - position.avg_price) * position.shares

    logger.info(
        "[%s] %sREDEEM %s | %.2f shares → $%.2f | P&L $%.2f | %s",
        algo.name, "PAPER " if paper else "", result, position.shares,
        proceeds, pnl, (intent.question or position.question)[:55],
    )

    trade = _intent_to_trade(
        intent, action="REDEEM",
        asset_id_override=position.asset_id,
        question=intent.question or position.question,
        outcome=intent.outcome or position.outcome,
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

    sign = "+" if pnl >= 0 else ""
    notifier.send(
        f"[{algo.name}] {'📄 PAPER ' if paper else ''}"
        f"{'✅ WIN' if result == 'WIN' else '❌ LOSS'}\n"
        f"{notifier._esc(position.outcome)} resolved | {position.shares:.2f} shares "
        f"→ ${proceeds:.2f} | P&L {sign}${pnl:.2f}\n"
        f"{notifier._esc((intent.question or position.question)[:80])}"
    )


# ---------------------------------------------------------------------------
# Intent → Trade adapter (keeps legacy interfaces unchanged)
# ---------------------------------------------------------------------------

def _intent_to_trade(
    intent: Intent,
    action: str,
    asset_id_override: Optional[str] = None,
    question: Optional[str] = None,
    outcome: Optional[str] = None,
) -> Trade:
    """Build a synthetic Trade from an Intent for tracker/notifier APIs.

    The Trade dataclass is what the existing PositionTracker, RiskManager,
    and notifier expect. Rather than refactor all three to take Intents
    directly (large blast radius), the runner wraps each Intent in a Trade
    on the way to those callees.

    `size_usdc` is set to the action-relevant magnitude when available:
      * OpenIntent → the BUY notional (used by risk.check_min_order indirectly)
      * CloseIntent / SettleIntent → 0 (sells/redeems skip size checks)
    """
    import time as _time
    market_id = getattr(intent, "market_id", "")
    raw_asset = getattr(intent, "asset_id", None)
    asset_id = asset_id_override or raw_asset
    price = getattr(intent, "signal_price", 0.0) or 0.0
    size = getattr(intent, "usdc_amount", 0.0) or 0.0
    return Trade(
        id=intent.signal_id or f"{action.lower()}-{int(_time.time())}",
        market_id=market_id,
        question=question if question is not None else intent.question,
        side=action,
        size_usdc=size,
        price=price,
        action=action,
        timestamp=int(_time.time()),
        outcome=outcome if outcome is not None else intent.outcome,
        asset_id=asset_id,
    )


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

    return fetcher.fetch_resolution_price(trade.asset_id)


def _slippage_ok(
    trade: Trade,
    current_price: Optional[float],
    max_slippage: float,
    algo_name: str,
) -> bool:
    """Fail closed when price data is unavailable."""
    if current_price is None:
        logger.warning(
            "[%s] No current price for slippage check — refusing: %s",
            algo_name, trade.question[:50],
        )
        notifier.on_slippage_skipped(trade, drift_pct=float("nan"), algo_name=algo_name)
        return False
    if trade.price <= 0:
        return True
    drift = abs(current_price - trade.price) / trade.price
    if drift > max_slippage:
        logger.warning(
            "[%s] Slippage %.1f%% > max %.1f%%, skipping: %s",
            algo_name, drift * 100, max_slippage * 100, trade.question[:50],
        )
        notifier.on_slippage_skipped(trade, drift * 100, algo_name=algo_name)
        return False
    return True


# ---------------------------------------------------------------------------
# Paper-mode fill simulation
# ---------------------------------------------------------------------------

def _simulate_buy(
    scaled_usdc: float, current_price: Optional[float], paper_fee_bps: float
) -> FillResult:
    """Simulate a market BUY at the current price, charging the modeled fee."""
    if current_price is None or current_price <= 0:
        return FillResult(False, 0, 0, 0, reason="no current price")
    fee = scaled_usdc * (paper_fee_bps / 10_000.0)
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


def _simulate_sell(
    shares: float, current_price: Optional[float], paper_fee_bps: float
) -> FillResult:
    if current_price is None or current_price <= 0:
        return FillResult(False, 0, 0, 0, reason="no current price")
    gross = shares * current_price
    fee = gross * (paper_fee_bps / 10_000.0)
    return FillResult(
        success=True,
        shares=shares,
        amount_usdc=gross,             # proceeds (gross of fee)
        fill_price=current_price,
        fee_usdc=fee,
    )


# ---------------------------------------------------------------------------
# Live order placement
# ---------------------------------------------------------------------------

def _place_buy(
    trade: Trade, scaled_usdc: float, client: ClobClient, order_type: str
) -> FillResult:
    try:
        if order_type == "market":
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
        # Log the raw response (repr) so a misparse can be diagnosed from the
        # logs alone — the field names `_parse_fill` accepts (takingAmount /
        # makingAmount and aliases) are inferred from py_clob_client docs, not
        # validated against a captured live response. Keep this log permanent
        # until at least one prod fill is verified end-to-end.
        logger.info("RAW BUY order response: %r", resp)
        return _parse_fill(resp, side="BUY")
    except Exception as e:
        logger.error("BUY order failed: %s", e)
        return FillResult(False, 0, 0, 0, reason=str(e))


def _place_sell(
    trade: Trade, shares: float, client: ClobClient, order_type: str
) -> FillResult:
    try:
        if order_type == "market":
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
        logger.info("RAW SELL order response: %r", resp)
        return _parse_fill(resp, side="SELL")
    except Exception as e:
        logger.error("SELL order failed: %s", e)
        return FillResult(False, 0, 0, 0, reason=str(e))


def _parse_fill(resp, side: str) -> FillResult:
    """Pull the actual fill out of a CLOB order response.

    Conservative posture: a success=True response with no parseable fill
    data is treated as failure, not silently trusted to match the request.
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

    taking_raw = resp.get("takingAmount") or resp.get("taker_amount") or resp.get("size_matched")
    making_raw = resp.get("makingAmount") or resp.get("maker_amount") or resp.get("filled_amount")
    try:
        taking = float(taking_raw) if taking_raw is not None else 0.0
        making = float(making_raw) if making_raw is not None else 0.0
    except (TypeError, ValueError):
        return FillResult(False, 0, 0, 0, reason="unparseable amounts")

    if taking <= 0 and making <= 0:
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


# ---------------------------------------------------------------------------
# REDEEM resolution price (Gamma + CLOB fallback)
# ---------------------------------------------------------------------------

def _resolve_close_price(market_id: str, asset_id: str) -> Optional[float]:
    """Determine the final price for a redeemed position.

    Strategy (in order):
      1. Ask Gamma for the market and *only* trust outcomePrices if the
         market is explicitly closed/resolved.
      2. Fall back to CLOB last-trade-price binarized to {0, 1}.
      3. If neither yields a confident answer, return None and leave the
         position open.
    """
    market = fetcher.fetch_market_resolution(market_id)
    if market and _market_is_resolved(market):
        outcome_price = _gamma_outcome_price(market, asset_id)
        if outcome_price is not None:
            return outcome_price

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


