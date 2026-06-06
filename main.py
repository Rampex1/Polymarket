#!/usr/bin/env python3
"""
Entry point — wires modules together and runs the poll loop.

Two notable changes vs. the previous version:

  * `paper` is resolved exactly once here (`paper_mode = ...`) and the result
    is the single source of truth used everywhere downstream. Previously some
    modules read `config.PAPER_TRADE` while others used a per-call argument,
    producing inconsistent behavior when callers expected one and got the other.

  * Shutdown is driven by a `threading.Event` rather than `sys.exit` from a
    signal handler. This guarantees we never interrupt an in-flight order
    placement, and the daily-summary thread also notices the shutdown.
"""

import logging
import signal
import sys
import threading

from bot import config, executor, fetcher, notifier
from bot.positions import PositionTracker, RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    # ── Resolve wallet address ──────────────────────────────────────────────
    address = config.TARGET_ADDRESS
    if not address:
        if not config.TARGET_USERNAME:
            # Fail loud — with TARGET_USERNAME no longer defaulted, an empty
            # config means the user forgot to set it. Don't silently make a
            # garbage HTTP call.
            logger.error(
                "Neither TARGET_ADDRESS nor TARGET_USERNAME is set in .env."
            )
            sys.exit(1)
        logger.info("Looking up wallet for '%s'...", config.TARGET_USERNAME)
        address = fetcher.lookup_wallet(config.TARGET_USERNAME)

    if not address:
        logger.error(
            "Could not resolve wallet for '%s'. Set TARGET_ADDRESS in .env.",
            config.TARGET_USERNAME or "<unset>",
        )
        sys.exit(1)

    logger.info("Monitoring address: %s", address)

    # Show recent trade history to confirm feed is live.
    logger.info("Fetching recent trade history...")
    recent = fetcher.fetch_recent_trades(address, limit=10)
    if recent:
        logger.info("Last %d trades:", len(recent))
        for t in recent[-5:]:
            print(f"  {t}")
    else:
        logger.warning("No recent trades found — address may be wrong or API is slow.")

    # ── Build execution stack ───────────────────────────────────────────────
    client = executor.build_client()
    # Resolve paper-mode exactly once so every module agrees.
    paper_mode = config.PAPER_TRADE or client is None

    tracker = PositionTracker()
    if paper_mode:
        tracker.init_paper_balance(config.PAPER_STARTING_BALANCE)
    risk = RiskManager(tracker)

    mode = "PAPER" if paper_mode else "LIVE"
    logger.info(
        "Execution mode: %s | tiers=$%.0f/$%.0f/$%.0f | order=%s | max_slippage=%.0f%%",
        mode, config.TIER1_SIZE, config.TIER2_SIZE, config.TIER3_SIZE,
        config.ORDER_TYPE.upper(), config.MAX_SLIPPAGE * 100,
    )
    logger.info(
        "Risk limits: per-position $%.2f | total exposure $%.2f | daily loss $%.2f "
        "| min order $%.2f",
        config.MAX_POSITION_SIZE_USDC,
        config.MAX_TOTAL_EXPOSURE_USDC,
        config.DAILY_LOSS_LIMIT_USDC,
        config.MIN_ORDER_SIZE_USDC,
    )

    tracker.print_summary(paper=paper_mode)

    # ── Shutdown coordination ───────────────────────────────────────────────
    # Use an Event instead of sys.exit() in a signal handler. This lets the
    # poll loop and daily-summary thread finish their current iteration
    # cleanly — critical so we don't interrupt mid-order.
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        if stop_event.is_set():
            return   # second Ctrl-C: let default handler kill us
        logger.info("Shutdown signal received, finishing current cycle...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Background threads ──────────────────────────────────────────────────
    # Pass paper_mode through so the daily summary filters out leftover rows
    # from the other mode (paper rows leaking into a live deployment, etc).
    notifier.start_daily_summary(tracker, paper=paper_mode, stop_event=stop_event)
    notifier.on_startup(mode, tracker.total_exposure_usdc(paper=paper_mode))

    # ── Poll loop ───────────────────────────────────────────────────────────
    logger.info(
        "Starting poll loop (every %ds, min target-trade size $%.2f)...",
        config.POLL_INTERVAL_SECONDS,
        config.MIN_TRADE_SIZE_USDC,
    )
    try:
        fetcher.poll(
            address,
            on_trade=lambda t: executor.execute(t, client, tracker, risk),
            stop_event=stop_event,
        )
    finally:
        tracker.print_summary(paper=paper_mode)
        notifier.on_shutdown()


if __name__ == "__main__":
    main()
