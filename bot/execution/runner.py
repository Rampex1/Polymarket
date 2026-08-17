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
  * Risk + ledger are algo-scoped (each thread gets its own instances).
"""

import logging
from typing import Optional

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds, MarketOrderArgs, OrderArgs, OrderPayload, OrderType,
)
from py_clob_client_v2.constants import POLYGON

from .. import config
from ..storage import orders, signals
from ..discord import alerts
from . import lots
from ..domain.intents import CloseIntent, Intent, OpenIntent, SettleIntent
from .fills import FillResult, simulate_buy as _simulate_buy, simulate_sell as _simulate_sell
from .pricing import current_price as _get_current_price, slippage_ok as _slippage_ok
from .settlement import resolve_close_price as _resolve_close_price
from ..domain.records import Trade
from ..storage.ledger import Ledger
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

def _notifies_signals(algo) -> bool:
    """Whether this algorithm wants pre-execution alerts (signals, risk
    blocks). Opt-out, read off params: an algorithm that never declares the
    knob keeps posting them, so adding it changed no existing behavior.
    Fills, settlements, and failures are never suppressed by it — those are
    what happened, not what was considered."""
    return getattr(algo.params, "notify_signals", True)


def dispatch(
    intent: Intent,
    algo,                     # bot.domain.algorithm.Algorithm
    ledger: Ledger,
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

    # A strategy that emits many more candidates than it can fill wants its
    # channel to be a trade log, not a firehose. Absent the knob, every
    # existing algorithm keeps posting exactly as before.
    if _notifies_signals(algo):
        alerts.on_signal(intent, algo.display_name, webhook_url=algo.params.webhook_url,
                           paper=paper)

    if isinstance(intent, OpenIntent):
        _handle_open(intent, algo, ledger, risk, client, paper)
    elif isinstance(intent, CloseIntent):
        _handle_close(intent, algo, ledger, client, paper)
    elif isinstance(intent, SettleIntent):
        _handle_settle(intent, algo, ledger, paper)


# ---------------------------------------------------------------------------
# OPEN — risk check, slippage gate, fill, record
# ---------------------------------------------------------------------------

def _handle_open(
    intent: OpenIntent,
    algo,
    ledger: Ledger,
    risk: RiskManager,
    client: Optional[ClobClient],
    paper: bool,
) -> None:
    if not intent.asset_id:
        logger.warning("OpenIntent missing asset_id, skipping: %s", intent.reason)
        return

    # Build a synthetic Trade so existing ledger/risk/alert APIs work
    # unchanged. (Trade is the legacy data class; intents are the new front-end.)
    trade = _intent_to_trade(intent, action="BUY")

    approved, reason = risk.check(trade, intent.usdc_amount, paper)
    if not approved:
        logger.warning("[%s] Risk check failed — %s | %s",
                       algo.name, reason, trade.question[:50])
        signals.record(algo.name, intent, paper, executed=False,
                       skip_reason=f"risk: {reason}")
        if "suspended" not in reason and _notifies_signals(algo):
            alerts.on_risk_blocked(reason, trade, algo.display_name,
                                     webhook_url=algo.params.webhook_url, paper=paper)
        return

    current_price = _get_current_price(trade, client)
    if not _slippage_ok(trade, current_price, algo.params.max_slippage, algo.display_name):
        signals.record(algo.name, intent, paper, executed=False,
                       skip_reason="slippage")
        return

    # A maker entry does not fill now, so it cannot be recorded now. It rests
    # on the book and `reconcile_open_orders` books it later, or cancels it.
    if _entry_style(algo) == "maker":
        _rest_maker_buy(intent, trade, algo, client, paper)
        return

    if paper:
        fill = _simulate_buy(intent.usdc_amount, current_price, algo.params.paper_fee_bps)
    else:
        fill = _place_buy(trade, intent.usdc_amount, client, algo.params.order_type)

    if not fill or not fill.success:
        reason_str = fill.reason if fill else "no fill"
        logger.warning("[%s] BUY did not fill (%s) — not recording.",
                       algo.name, reason_str)
        alerts.on_buy_failed(trade, reason_str, algo.display_name, webhook_url=algo.params.webhook_url,
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

    _book_fill(intent, trade, fill, algo, ledger, paper)


def _book_fill(intent: OpenIntent, trade: Trade, fill: FillResult,
               algo, ledger: Ledger, paper: bool) -> None:
    """Record a BUY that actually filled. Shared by the taker and maker paths
    so a maker fill can never drift from a taker one."""
    signals.record(algo.name, intent, paper, executed=True)
    ledger.record_buy(
        trade,
        spent_usdc=fill.amount_usdc,
        shares=fill.shares,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    if intent.source:
        lots.record_open(
            algo.name, intent.market_id, trade.asset_id or "", intent.source,
            intent.source_event_id or intent.signal_id, fill.shares, fill.amount_usdc,
        )
    alerts.on_buy_executed(trade, fill.amount_usdc, paper, fill.fill_price, algo.display_name, webhook_url=algo.params.webhook_url)
    ledger.print_summary(paper=paper)


# ---------------------------------------------------------------------------
# OPEN as a maker — rest on the book instead of crossing it
# ---------------------------------------------------------------------------
#
# Why this exists: Polymarket charges takers `shares * theta * p * (1-p)` and
# charges makers nothing, on top of the half-spread a taker pays to cross.
# Measured across 23,934 closed markets, every directional edge available is
# ~1 point gross while taker drag runs 1-3 points — so the same trade that
# loses as a taker can pay as a maker. The cost of finding out is non-fills,
# which is exactly what this path has to model honestly rather than assume
# away.

def _entry_style(algo) -> str:
    """'taker' unless the algorithm opts in. Absent knob → unchanged behaviour
    for every algorithm that predates this."""
    return str(getattr(algo.params, "entry_style", "taker") or "taker").lower()


def _maker_limit(intent: OpenIntent, client: Optional[ClobClient],
                 paper: bool) -> Optional[float]:
    """The price to rest at: the best bid, improved by a tick when there is
    room to do so and still not cross.

    Resting at the bid is what makes us the maker. Improving by one tick buys
    queue position without giving up that status; `post_only` on the order is
    what actually guarantees it, so a stale book here costs a rejection rather
    than a silent taker fill.
    """
    bid = ask = tick = None
    if client is not None and intent.asset_id:
        try:
            book = client.get_order_book(intent.asset_id)
            bids = getattr(book, "bids", None) or []
            asks = getattr(book, "asks", None) or []
            if bids:
                bid = max(float(b.price) for b in bids)
            if asks:
                ask = min(float(a.price) for a in asks)
            tick = float(client.get_tick_size(intent.asset_id))
        except Exception as e:
            logger.warning("Order book lookup failed for %s: %s",
                           (intent.asset_id or "")[:16], e)

    # Paper has no book to read, and a live lookup can fail. The screen already
    # recorded both sides, so fall back to what it saw.
    if bid is None:
        try:
            bid = float(intent.features.get("bid"))
        except (TypeError, ValueError, AttributeError):
            return None
    if ask is None:
        ask = float(intent.signal_price or 0.0) or None
    if tick is None:
        tick = 0.001

    if bid <= 0 or bid >= 1:
        return None
    improved = round(bid + tick, 6)
    if ask is not None and improved >= ask:
        improved = bid
    return improved


def _rest_maker_buy(intent: OpenIntent, trade: Trade, algo,
                    client: Optional[ClobClient], paper: bool) -> None:
    price = _maker_limit(intent, client, paper)
    if price is None or price <= 0:
        logger.warning("[%s] No usable bid for a maker entry on %s — skipping.",
                       algo.name, (intent.question or intent.market_id)[:50])
        signals.record(algo.name, intent, paper, executed=False,
                       skip_reason="maker: no bid")
        return

    size = intent.usdc_amount / price
    order_id = None

    if paper:
        # No order is placed. The row is a claim that we *would* be resting at
        # this price, and reconcile fills it only if the market actually trades
        # there — an optimistic model (it ignores queue position) but a real
        # one, unlike assuming the fill.
        order_id = f"paper-{algo.name}-{intent.signal_id or intent.market_id}"
    else:
        if client is None:
            logger.error("[%s] Live maker entry with no CLOB client.", algo.name)
            return
        try:
            args = OrderArgs(token_id=trade.asset_id, price=price,
                             size=size, side="BUY")
            signed = client.create_order(args)
            # post_only is the whole point: the exchange rejects the order
            # outright if it would cross and take, so maker status is
            # guaranteed by the venue rather than inferred from our price.
            resp = client.post_order(signed, OrderType.GTC, post_only=True)
            logger.info("RAW maker BUY response: %r", resp)
        except Exception as e:
            logger.error("[%s] Maker BUY failed to post: %s", algo.name, e)
            signals.record(algo.name, intent, paper, executed=False,
                           skip_reason=f"maker: post failed: {e}")
            return

        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id")
            status = (resp.get("status") or "").lower()
            if status in ("rejected", "unmatched", "expired") or resp.get("success") is False:
                # Usually post_only refusing to let us take. Not a failure
                # worth suspending the market over — the book simply moved.
                logger.info("[%s] Maker BUY not accepted (%s) — book moved.",
                            algo.name, status or "rejected")
                signals.record(algo.name, intent, paper, executed=False,
                               skip_reason=f"maker: {status or 'rejected'}")
                return
        if not order_id:
            logger.warning("[%s] Maker BUY accepted but returned no order id.",
                           algo.name)
            signals.record(algo.name, intent, paper, executed=False,
                           skip_reason="maker: no order id")
            return

    orders.record(order_id, algo.name, paper, intent, price, size)
    signals.record(algo.name, intent, paper, executed=False,
                   skip_reason="maker: resting")
    logger.info("[%s] Resting %.4f shares @ %.4f (ask was %.4f) on %s",
                algo.name, size, price, intent.signal_price or 0.0,
                (intent.question or intent.market_id)[:50])


def reconcile_open_orders(algo, ledger: Ledger, client: Optional[ClobClient],
                          paper: bool) -> None:
    """Book maker fills, and cancel orders that have gone stale.

    Called once per poll from the worker loop. A no-op for any algorithm that
    has not opted into maker entry, so this costs a dict lookup for everyone
    else.
    """
    if _entry_style(algo) != "maker":
        return
    ttl = float(getattr(algo.params, "maker_ttl_seconds", 300.0))
    for order in orders.resting(algo.name, paper):
        try:
            _reconcile_one(order, algo, ledger, client, paper, ttl)
        except Exception:
            logger.exception("[%s] Reconcile failed for order %s",
                             algo.name, order.order_id[:20])


def _reconcile_one(order, algo, ledger: Ledger, client: Optional[ClobClient],
                   paper: bool, ttl: float) -> None:
    intent = order.intent
    trade = _intent_to_trade(intent, action="BUY")
    expired = order.age_seconds() > ttl

    if paper:
        # Filled only if the market has actually traded at or through our
        # limit since we posted.
        price = _get_current_price(trade, client)
        if price and price <= order.limit_price:
            fill = FillResult(True, order.size, order.size * order.limit_price,
                              order.limit_price, fee_usdc=0.0)
            orders.drop(order.order_id, algo.name)
            _book_fill(intent, trade, fill, algo, ledger, paper)
            return
        if expired:
            orders.drop(order.order_id, algo.name)
            signals.record(algo.name, intent, paper, executed=False,
                           skip_reason="maker: unfilled")
            logger.info("[%s] Maker order expired unfilled @ %.4f on %s",
                        algo.name, order.limit_price,
                        (intent.question or intent.market_id)[:50])
        return

    if client is None:
        return
    matched_shares = matched_usdc = 0.0
    status = ""
    try:
        row = client.get_order(order.order_id)
        if isinstance(row, dict):
            status = (row.get("status") or "").lower()
            matched_shares = float(row.get("size_matched") or 0.0)
            fill_price = float(row.get("price") or order.limit_price)
            matched_usdc = matched_shares * fill_price
    except Exception as e:
        logger.warning("[%s] get_order(%s) failed: %s",
                       algo.name, order.order_id[:16], e)
        return

    done = status in ("matched", "filled", "cancelled", "canceled", "expired")
    if expired and not done:
        try:
            client.cancel_order(OrderPayload(orderID=order.order_id))
            logger.info("[%s] Cancelled stale maker order %s (%.0fs old)",
                        algo.name, order.order_id[:16], order.age_seconds())
        except Exception as e:
            logger.warning("[%s] cancel_order(%s) failed: %s",
                           algo.name, order.order_id[:16], e)
            return
        done = True

    if matched_shares > 0 and (done or matched_shares >= order.size - 1e-9):
        fill_price = matched_usdc / matched_shares if matched_shares else order.limit_price
        orders.drop(order.order_id, algo.name)
        _book_fill(intent, trade,
                   FillResult(True, matched_shares, matched_usdc, fill_price,
                              fee_usdc=0.0),
                   algo, ledger, paper)
    elif done:
        orders.drop(order.order_id, algo.name)
        signals.record(algo.name, intent, paper, executed=False,
                       skip_reason=f"maker: {status or 'cancelled'} unfilled")


# ---------------------------------------------------------------------------
# CLOSE — fractional sell of our position
# ---------------------------------------------------------------------------

def _handle_close(
    intent: CloseIntent,
    algo,
    ledger: Ledger,
    client: Optional[ClobClient],
    paper: bool,
) -> None:
    position = ledger.get(intent.market_id, paper)
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
    ledger.record_sell(
        trade,
        shares=fill.shares,
        proceeds_usdc=fill.amount_usdc,
        fill_price=fill.fill_price,
        paper=paper,
        fee_usdc=fill.fee_usdc,
    )
    if intent.source:
        lots.close_for_source(
            algo.name, intent.market_id, intent.source, fill.shares,
        )
    alerts.on_sell_executed(trade, fill.shares, pnl, paper, fill.fill_price, algo.display_name, webhook_url=algo.params.webhook_url)
    ledger.print_summary(paper=paper)


# ---------------------------------------------------------------------------
# SETTLE — market resolved, book at canonical close price
# ---------------------------------------------------------------------------

def _handle_settle(
    intent: SettleIntent,
    algo,
    ledger: Ledger,
    paper: bool,
) -> None:
    position = ledger.get(intent.market_id, paper)
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
    ledger.record_sell(
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
    lots.close_market(algo.name, intent.market_id)
    ledger.print_summary(paper=paper)

    alerts.on_settle_executed(
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
    """Build a synthetic Trade from an Intent for ledger/alert APIs.

    The Trade dataclass is what the existing Ledger, RiskManager,
    and alerts expect. Rather than refactor all three to take Intents
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


def _place_order(
    side: str, trade: Trade, amount: float, client: ClobClient, order_type: str
) -> FillResult:
    """Place one order and return what actually filled.

    `amount` is USDC for a BUY and shares for a SELL — the CLOB's market
    order takes the currency you are giving up.

    Both sides share this path deliberately: they previously diverged, and
    the SELL leg silently skipped the delayed-order poll, which reports a
    matched sell as a no-fill and leaves a phantom position on the books.
    """
    try:
        if order_type == "market":
            # price=0 → SDK calls calculate_market_price and prices the
            # order at the level needed to fill against current book depth.
            # The bot's slippage gate (run upstream) already bounded drift
            # vs signal_price, so this won't walk further than tolerated.
            args = MarketOrderArgs(
                token_id=trade.asset_id,
                amount=amount,
                side=side,
            )
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FAK)
        else:
            # A limit BUY is sized in USDC; the exchange wants shares.
            size = amount / trade.price if side == "BUY" else amount
            args = OrderArgs(
                token_id=trade.asset_id,
                price=trade.price,
                size=size,
                side=side,
            )
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)

        # Log the raw response (repr) so a misparse can be diagnosed from the
        # logs alone. The making/taking direction in `_parse_fill` was
        # validated against prod fills + on-chain balances on 2026-06-12;
        # the snake_case aliases remain unverified — keep this log until
        # they have been seen in the wild too.
        logger.info("RAW %s order response: %r", side, resp)

        # The CLOB matching engine is async — small orders sometimes land with
        # status='delayed' and empty takingAmount/makingAmount. Poll get_order()
        # until the order resolves (matched/live/cancelled) before parsing.
        resp = _resolve_delayed(resp, client)

        return _parse_fill(resp, side=side)
    except Exception as e:
        logger.error("%s order failed: %s", side, e)
        return FillResult(False, 0, 0, 0, reason=str(e))


def _place_buy(
    trade: Trade, scaled_usdc: float, client: ClobClient, order_type: str
) -> FillResult:
    return _place_order("BUY", trade, scaled_usdc, client, order_type)


def _place_sell(
    trade: Trade, shares: float, client: ClobClient, order_type: str
) -> FillResult:
    return _place_order("SELL", trade, shares, client, order_type)


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
