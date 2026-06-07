#!/usr/bin/env python3
"""
Entry point — boots one worker thread per enabled algorithm.

Architecture
------------
Each algorithm in `algorithms.ENABLED` runs as a fully independent worker:

  * Own `PositionTracker(algo=...)` — DB rows partitioned by algo.
  * Own `RiskManager(tracker, params)` — caps come from algo params,
    not shared global config.
  * Own polling cadence (`params.poll_interval_seconds`).
  * Own paper bankroll (per-algo row in `paper_account`).
  * Own daily-summary thread tagged with algo name.

The only shared infrastructure is the CLOB client (one wallet, one
connection), the SQLite connection (thread-local), the notifier, and the
HTTP session used by `bot.fetcher`. Crashes in one algorithm do not affect
the others — each worker catches its own exceptions per tick.
"""

import logging
import signal
import sys
import threading
from typing import Optional

from algorithms import ENABLED
from bot import config, notifier, runner
from bot.algorithm import Algorithm
from bot.positions import PositionTracker, RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _run_worker(
    algo: Algorithm,
    client,
    paper: bool,
    stop_event: threading.Event,
) -> None:
    """One algorithm's lifecycle: setup → poll loop → shutdown."""
    name = algo.params.name
    try:
        tracker = PositionTracker(algo=name)
        if paper:
            tracker.init_paper_balance(algo.params.paper_starting_balance)
        risk = RiskManager(tracker, algo.params)
        algo.setup(tracker, notifier, client)

        notifier.start_daily_summary(
            tracker, paper=paper, stop_event=stop_event, algo_name=name,
        )
        notifier.on_startup(
            "PAPER" if paper else "LIVE",
            tracker.total_exposure_usdc(paper=paper),
            algo_name=name,
        )

        logger.info(
            "[%s] Started — poll every %ds | risk: per-position $%.2f, "
            "total $%.2f, daily loss $%.2f | slippage %.0f%% | order=%s",
            name, algo.params.poll_interval_seconds,
            algo.params.max_position_size_usdc,
            algo.params.max_total_exposure_usdc,
            algo.params.daily_loss_limit_usdc,
            algo.params.max_slippage * 100,
            algo.params.order_type.upper(),
        )
        tracker.print_summary(paper=paper)

        while not stop_event.is_set():
            try:
                for intent in algo.poll():
                    if stop_event.is_set():
                        break
                    runner.dispatch(intent, algo, tracker, risk, client, paper)
            except Exception:
                # One bad tick must not kill this algorithm. Log and keep
                # going — siblings continue regardless.
                logger.exception("[%s] poll/dispatch raised, continuing", name)
            stop_event.wait(algo.params.poll_interval_seconds)
    finally:
        try:
            tracker = PositionTracker(algo=name)
            tracker.print_summary(paper=paper)
        except Exception:
            pass
        notifier.on_shutdown(algo_name=name)
        logger.info("[%s] Stopped.", name)


def main() -> None:
    if not ENABLED:
        logger.error("No algorithms enabled in algorithms/__init__.py")
        sys.exit(1)

    client = runner.build_client()
    paper = config.PAPER_TRADE or client is None

    logger.info(
        "Execution mode: %s | algorithms enabled: %s",
        "PAPER" if paper else "LIVE",
        ", ".join(a.params.name for a in ENABLED),
    )

    # Single Event coordinates shutdown for every worker + daily summary.
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        if stop_event.is_set():
            return   # second Ctrl-C: let default handler kill us
        logger.info("Shutdown signal received, finishing current cycles...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    workers = [
        threading.Thread(
            target=_run_worker, args=(algo, client, paper, stop_event),
            daemon=True, name=f"worker-{algo.params.name}",
        )
        for algo in ENABLED
    ]
    for w in workers:
        w.start()

    # Block the main thread until shutdown is signaled. Worker threads
    # are daemons so they'd be killed at process exit even without join();
    # the wait gives them a chance to finish their current cycle cleanly.
    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    finally:
        for w in workers:
            w.join(timeout=30)


if __name__ == "__main__":
    main()
