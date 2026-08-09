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

This module is the compatibility-facing dispatcher.  Pure pricing, paper
fills, and settlement-price selection live under ``bot.execution``; live
CLOB placement remains here because it is the only stateful exchange edge.

What changed:
  * Slippage cap and order type come from `algo.params`, not module config.
  * Risk + tracker are algo-scoped (each thread gets its own instances).
"""

import logging
from typing import Optional

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
from py_clob_client_v2.constants import POLYGON

from . import config, signals
from .discord import notifier
from .execution import lots as copy_lots
from .domain.intents import CloseIntent, Intent, OpenIntent, SettleIntent
from .execution.fills import FillResult, simulate_buy as _simulate_buy, simulate_sell as _simulate_sell
from .execution.pricing import current_price as _get_current_price, slippage_ok as _slippage_ok
from .execution.settlement import resolve_close_price as _resolve_close_price
from .domain.records import Trade
from .positions import PositionTracker
from .risk import RiskManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLOB client construction
# ---------------------------------------------------------------------------

def build_client() -> Optional[ClobClient]:
    if not config.POLY_PRIVATE_KEY:
        logger.error("POLY_PRIVATE_KEY not set — cannot build a CLOB client.")
        return None

    # Both signature_type 1 (email/Magic) and 2 (browser wallet) route
    # orders through a Polymarket proxy that holds USDC. Without an explicit
    # funder the client defaults to the EOA — orders sign correctly but USDC
    # is debited from the wrong account, producing silent ghost positions on
    # the user's actual proxy wallet.
    if not config.POLY_FUNDER_ADDRESS:
        logger.error(
            "POLY_FUNDER_ADDRESS is required for live trading (your Polymarket "
            "proxy wallet, visible in the profile URL). Refusing to build a "
            "client — orders would debit the wrong account."
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

    logger.info(
        "Building CLOB client (signature_type=%d, funder=%s).",
        config.POLY_SIGNATURE_TYPE, config.POLY_FUNDER_ADDRESS,
    )
    return ClobClient(
        host=config.CLOB_API,
        key=config.POLY_PRIVATE_KEY,
        chain_id=POLYGON,
        creds=creds,
        signature_type=config.POLY_SIGNATURE_TYPE,
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
    if isinstance(intent, OpenIntent) and risk.is_suspended(intent.market_id):
        return  # prior BUY failed — suppress all further signals silently

    notifier.on_signal(intent, algo.display_name, webhook_url=algo.params.webhook_url,
                       paper=paper)

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
        signals.record(algo.name, intent, paper, executed=False,
                       skip_reason=f"risk: {reason}")
        if "suspended" not in reason:
            notifier.on_risk_blocked(reason, trade, algo.display_name,
                                     webhook_url=algo.params.webhook_url, paper=paper)
        return

    current_price = _get_current_price(trade, client)
    if not _slippage_ok(trade, current_price, algo.params.max_slippage, algo.display_name):
        signals.record(algo.name, intent, paper, executed=False,
                       skip_reason="slippage")
        return

    if paper:
        fill = _simulate_buy(intent.usdc_amount, current_price, algo.params.paper_fee_bps)
    else:
        fill = _place_buy(trade, intent.usdc_amount, client, algo.params.order_type)

    if not fill or not fill.success:
        reason_str = fill.reason if fill else "no fill"
        logger.warning("[%s] BUY did not fill (%s) — not recording.",
                       algo.name, reason_str)
        notifier.on_buy_failed(trade, reason_str, algo.display_name, webhook_url=algo.params.webhook_url,
                               paper=paper)
        signals.record(algo.name, intent, paper, executed=False,
                       skip_reason=f"no fill: {reason_str}")
        # Suspend further buy attempts for this market this session.
        # dispatch() will silently drop all subsequent OpenIntents for it,
        # preventing retry loops and Discord spam after any failed fill.
        risk.suspend_market(intent.market_id)
        logger.warning("[%s] BUY suspended for %s until restart.",
                       algo.name, (intent.question or intent.market_id)[:55])
        return

    signals.record(algo.name, intent, paper, executed=True)
    tracker.record_buy(
        trade,
        spent_usdc=fill.amount_usdc,
        shares=fill.shares,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    if intent.leader_wallet:
        copy_lots.record_open(
            algo.name, intent.market_id, trade.asset_id or "", intent.leader_wallet,
            intent.leader_event_id or intent.signal_id, fill.shares, fill.amount_usdc,
        )
    notifier.on_buy_executed(trade, fill.amount_usdc, paper, fill.fill_price, algo.display_name, webhook_url=algo.params.webhook_url)
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
        if not _slippage_ok(trade, current_price, algo.params.max_slippage, algo.display_name):
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
    if intent.leader_wallet:
        copy_lots.close_for_leader(
            algo.name, intent.market_id, intent.leader_wallet, fill.shares,
        )
    notifier.on_sell_executed(trade, fill.shares, pnl, paper, fill.fill_price, algo.display_name, webhook_url=algo.params.webhook_url)
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
    # Backfill the outcome label on this market's signal rows — settlement
    # is the moment a logged signal becomes a labeled training example.
    signals.label_outcomes(algo.name, intent.market_id, close_price, pnl, paper)
    copy_lots.close_market(algo.name, intent.market_id)
    tracker.print_summary(paper=paper)

    notifier.on_settle_executed(
        market_id=intent.market_id,
        question=intent.question or position.question,
        outcome=position.outcome,
        shares=position.shares,
        proceeds=proceeds,
        pnl=pnl,
        close_price=close_price,
        paper=paper,
        algo_name=algo.display_name,
        webhook_url=algo.params.webhook_url,
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
# Live order placement
# ---------------------------------------------------------------------------

def _resolve_delayed(resp: dict, client: ClobClient, retries: int = 6, wait: float = 2.0) -> dict:
    """If the CLOB returns status='delayed', poll get_order() until resolved.

    'delayed' means the matching engine queued the order — takingAmount and
    makingAmount are empty strings until it matches or is cancelled. We wait
    up to retries*wait seconds (default 12s) before giving up and returning
    the original response (which _parse_fill will then treat as no-fill).

    get_order() returns a different schema than post_order():
      post_order: takingAmount (shares), makingAmount (USDC)
      get_order:  size_matched (shares), price (per share)
    We normalise the get_order response into the post_order shape so
    _parse_fill can handle both without branching.
    """
    import time as _time
    if not isinstance(resp, dict):
        return resp
    if (resp.get("status") or "").lower() != "delayed":
        return resp
    order_id = resp.get("orderID") or resp.get("order_id")
    if not order_id:
        return resp
    logger.info("Order %s is delayed — polling for resolution (up to %.0fs)...",
                order_id[:16], retries * wait)
    for attempt in range(retries):
        _time.sleep(wait)
        try:
            order = client.get_order(order_id)
            logger.info("Order %s status check %d: %r", order_id[:16], attempt + 1, order)
            if not isinstance(order, dict):
                continue
            status = (order.get("status") or "").lower()
            if status in ("delayed", ""):
                continue
            # Normalise get_order schema → post_order schema for _parse_fill.
            # get_order gives size_matched (shares) + price (per share);
            # _parse_fill expects takingAmount (shares) + makingAmount (USDC).
            size_matched = order.get("size_matched")
            price_str = order.get("price")
            if size_matched and price_str and not order.get("takingAmount"):
                try:
                    shares = float(size_matched)
                    usdc = shares * float(price_str)
                    order["takingAmount"] = str(shares)
                    order["makingAmount"] = str(usdc)
                except (TypeError, ValueError):
                    pass
            return order
        except Exception as e:
            logger.warning("get_order(%s) failed (attempt %d): %s", order_id[:16], attempt + 1, e)
    logger.warning("Order %s still delayed after %d attempts — treating as no-fill.", order_id[:16], retries)
    return resp


def _place_buy(
    trade: Trade, scaled_usdc: float, client: ClobClient, order_type: str
) -> FillResult:
    try:
        if order_type == "market":
            # price=0 → SDK calls calculate_market_price and prices the
            # order at the level needed to fill against current book depth.
            # The bot's slippage gate (run upstream) already bounded drift
            # vs signal_price, so this won't walk further than tolerated.
            args = MarketOrderArgs(
                token_id=trade.asset_id,
                amount=scaled_usdc,
                side="BUY",
            )
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FAK)
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
        # logs alone. The making/taking direction in `_parse_fill` was
        # validated against prod fills + on-chain balances on 2026-06-12;
        # the snake_case aliases remain unverified — keep this log until
        # they have been seen in the wild too.
        logger.info("RAW BUY order response: %r", resp)

        # The CLOB matching engine is async — small orders sometimes land with
        # status='delayed' and empty takingAmount/makingAmount. Poll get_order()
        # until the order resolves (matched/live/cancelled) before parsing.
        resp = _resolve_delayed(resp, client)

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
            )
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FAK)
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
        # Log at ERROR so the raw response can be inspected in the tmux buffer
        # and the field aliases extended if the CLOB changes its response shape.
        logger.error(
            "CLOB returned success but no parseable fill amounts — "
            "order may have silently filled on-chain. Full response: %r", resp,
        )
        return FillResult(
            False, 0, 0, 0,
            reason="success but no fill amounts in response",
        )

    # For an order WE created: makingAmount is what we give, takingAmount
    # is what we receive. Validated against live fills 2026-06-12 — the
    # inverse mapping recorded $1 tier-1 buys as "1.0 shares" with
    # impossible >1.0 avg prices (shares/cost transposed vs on-chain).
    if side == "BUY":
        spent = making        # we give USDC
        shares = taking       # we receive tokens
    else:
        spent = taking        # we receive USDC
        shares = making       # we give tokens

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
