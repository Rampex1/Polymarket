#!/usr/bin/env python3

import logging
import signal
import sys

import config
import executor
import fetcher
import notifier
from positions import PositionTracker, RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    # Resolve wallet address
    address = config.TARGET_ADDRESS
    if not address:
        logger.info("Looking up wallet for '%s'...", config.TARGET_USERNAME)
        address = fetcher.lookup_wallet(config.TARGET_USERNAME)

    if not address:
        logger.error(
            "Could not resolve wallet for '%s'. Set TARGET_ADDRESS in .env.",
            config.TARGET_USERNAME,
        )
        sys.exit(1)

    logger.info("Monitoring address: %s", address)

    # Show recent trade history to confirm feed is live
    logger.info("Fetching recent trade history...")
    recent = fetcher.fetch_recent_trades(address, limit=10)
    if recent:
        logger.info("Last %d trades:", len(recent))
        for t in recent[-5:]:
            print(f"  {t}")
    else:
        logger.warning("No recent trades found — address may be wrong or API is slow.")

    # Build execution stack
    client = executor.build_client()
    tracker = PositionTracker()
    risk = RiskManager(tracker)

    mode = "PAPER" if (config.PAPER_TRADE or client is None) else "LIVE"
    logger.info(
        "Execution mode: %s | scale=%.0f%% | order=%s | max_slippage=%.0f%%",
        mode, config.SCALE_FACTOR * 100, config.ORDER_TYPE.upper(),
        config.MAX_SLIPPAGE * 100,
    )
    logger.info(
        "Risk limits: per-position $%.0f | total exposure $%.0f | daily loss $%.0f",
        config.MAX_POSITION_SIZE_USDC,
        config.MAX_TOTAL_EXPOSURE_USDC,
        config.DAILY_LOSS_LIMIT_USDC,
    )

    tracker.print_summary()

    # Graceful shutdown on SIGINT / SIGTERM
    def _shutdown(signum, frame):
        logger.info("Shutdown signal received, exiting...")
        tracker.print_summary()
        notifier.on_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Start daily summary background thread
    notifier.start_daily_summary(tracker)

    # Notify startup
    notifier.on_startup(mode, config.SCALE_FACTOR * 100, tracker.total_exposure_usdc())

    # Start polling
    logger.info(
        "Starting poll loop (every %ds, min size $%.0f)...",
        config.POLL_INTERVAL_SECONDS,
        config.MIN_TRADE_SIZE_USDC,
    )
    fetcher.poll(
        address,
        on_trade=lambda t: executor.execute(t, client, tracker, risk),
    )


if __name__ == "__main__":
    main()
