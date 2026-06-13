#!/usr/bin/env python3
"""
Entry point — boots one worker thread per enabled algorithm.

Mode resolution
---------------
There is no global paper/live flag anymore. Each algorithm declares its
mode in its params (`Mode.PAPER` / `Mode.LIVE`). The runner threads pick
up that mode independently, so a single process can run prod-live and
paper-experimental algorithms side by side.

The shared CLOB client is built once, and only if at least one algorithm
is `Mode.LIVE`. If every algorithm is paper, no creds are needed.

Architecture
------------
Each algorithm in `algorithms.ENABLED` (selected by the `PROFILE` env var)
runs as a fully independent worker:

  * Own `PositionTracker(algo=...)` — DB rows partitioned by algo.
  * Own `RiskManager(tracker, params)` — caps come from algo params.
  * Own polling cadence (`params.poll_interval_seconds`).
  * Own paper bankroll (per-algo row in `paper_account`).
  * Own daily-summary thread tagged with algo name.

Crashes in one algorithm do not affect the others.
"""

import logging
import signal
import sys
import threading

from bot.profile_loader import ProfileError

try:
    from algorithms import ENABLED
except ProfileError as e:
    raise SystemExit(f"error: {e}") from None

from bot import config, db, notifier, reconciliation, runner, runs
from bot.algorithm import Algorithm, Mode
from bot.positions import PositionTracker, RiskManager


# Run a reconciliation every N poll cycles. With the default 20s poll
# interval, 30 cycles ≈ 10 minutes — frequent enough to catch drift
# quickly without hammering the Data API.
RECONCILE_EVERY_N_POLLS = 30

# Alert on Discord after this many consecutive poll-loop exceptions.
# At the default 20s interval, 5 failures ≈ 100 s of continuous errors.
# Subsequent alerts fire every CRASH_ALERT_AFTER_N_ERRORS failures so
# the channel isn't spammed if the problem persists.
CRASH_ALERT_AFTER_N_ERRORS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _run_worker(
    algo: Algorithm,
    client,
    stop_event: threading.Event,
) -> None:
    """One algorithm's lifecycle: setup → poll loop → shutdown."""
    name = algo.params.name
    # Mode comes from THIS algorithm's params. If params say LIVE but no
    # CLOB client exists (no creds), force paper to avoid silent misroutes.
    paper = algo.params.mode == Mode.PAPER or client is None
    if algo.params.mode == Mode.LIVE and client is None:
        logger.warning(
            "[%s] declared LIVE but no CLOB client available — falling back to PAPER.",
            name,
        )

    try:
        tracker = PositionTracker(algo=name)
        if paper:
            tracker.init_paper_balance(algo.params.paper_starting_balance)
        # Provenance: stamp this boot's resolved params + git sha into the
        # runs table so analytics can attribute results to config versions.
        runs.record_run(algo.params, config.PROFILE)
        risk = RiskManager(tracker, algo.params)
        algo.setup(tracker, notifier, client)

        display_name = algo.display_name
        webhook_url = algo.params.webhook_url
        notifier.on_startup(
            "PAPER" if paper else "LIVE",
            tracker.total_exposure_usdc(paper=paper),
            algo_name=display_name,
            webhook_url=webhook_url,
        )

        logger.info(
            "[%s] Started (%s) — poll every %ds | risk: per-position $%.2f, "
            "total $%.2f, daily loss $%.2f | slippage %.0f%% | order=%s",
            name, "PAPER" if paper else "LIVE",
            algo.params.poll_interval_seconds,
            algo.params.max_position_size_usdc,
            algo.params.max_total_exposure_usdc,
            algo.params.daily_loss_limit_usdc,
            algo.params.max_slippage * 100,
            algo.params.order_type.upper(),
        )
        tracker.print_summary(paper=paper)

        # Startup reconciliation — surfaces ghost positions or stale DB rows
        # before we start acting. Live-only; paper has nothing to reconcile.
        reconciliation.reconcile_positions(
            tracker, config.POLY_FUNDER_ADDRESS, algo.display_name, paper,
        )

        poll_count = 0
        consecutive_errors = 0
        while not stop_event.is_set():
            try:
                for intent in algo.poll():
                    if stop_event.is_set():
                        break
                    runner.dispatch(intent, algo, tracker, risk, client, paper)
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                logger.exception("[%s] poll/dispatch raised, continuing", name)
                if consecutive_errors % CRASH_ALERT_AFTER_N_ERRORS == 0:
                    notifier.on_worker_unstable(
                        display_name, consecutive_errors, exc,
                        webhook_url=webhook_url,
                    )

            # Periodic reconciliation. Failures inside reconcile_positions
            # only log; they never abort the worker.
            poll_count += 1
            if not paper and poll_count % RECONCILE_EVERY_N_POLLS == 0:
                try:
                    reconciliation.reconcile_positions(
                        tracker, config.POLY_FUNDER_ADDRESS, algo.display_name, paper,
                    )
                except Exception:
                    logger.exception("[%s] reconciliation raised, continuing", name)

            stop_event.wait(algo.params.poll_interval_seconds)
    finally:
        try:
            tracker = PositionTracker(algo=name)
            tracker.print_summary(paper=paper)
        except Exception:
            pass
        notifier.on_shutdown(algo_name=algo.display_name, webhook_url=algo.params.webhook_url)
        logger.info("[%s] Stopped.", name)


def _start_profile_summary(
    algos,
    client,
    stop_event: threading.Event,
) -> None:
    """Start a single daily summary thread for the whole profile.

    Sends to the profile-level summary webhook (config/webhooks.toml) at
    midnight. Creates fresh PositionTracker instances at send time so it
    doesn't hold live references to worker state.
    """
    summary_webhook = config.resolve_summary_webhook(config.PROFILE)
    if not summary_webhook:
        logger.info("No summary webhook configured for profile '%s' — skipping daily summary.", config.PROFILE)
        return

    algo_infos = [
        (a.params.name, a.params.mode == Mode.PAPER or client is None)
        for a in algos
    ]

    def _loop() -> None:
        while True:
            wait_s = notifier._seconds_until_midnight()
            if stop_event.wait(wait_s):
                return
            try:
                notifier.send_profile_summary(algo_infos, summary_webhook, config.PROFILE)
            except Exception:
                logger.exception("Profile daily summary raised, continuing")

    t = threading.Thread(target=_loop, daemon=True, name="daily-summary")
    t.start()
    logger.info("Profile daily summary thread started (profile=%s).", config.PROFILE)


def _warn_orphaned_algos() -> None:
    """Warn if the DB holds open positions under names absent from the
    profile — catches accidental renames in config/<profile>.toml, which
    would silently orphan a paper bankroll and its position history."""
    try:
        enabled_names = {a.params.name for a in ENABLED}
        rows = db.get().execute(
            "SELECT DISTINCT algo FROM positions WHERE shares > 0"
        ).fetchall()
        for (orphan,) in rows:
            if orphan not in enabled_names:
                logger.warning(
                    "DB has open positions under algo '%s', which is not in "
                    "profile '%s' — renamed or removed in config? Its "
                    "positions and paper bankroll are now unmanaged.",
                    orphan, config.PROFILE,
                )
    except Exception:
        logger.exception("orphan-name check failed (continuing)")


def main() -> None:
    if not ENABLED:
        logger.error("No algorithms enabled (profile=%s).", config.PROFILE)
        sys.exit(1)

    _warn_orphaned_algos()

    # Build the CLOB client only if at least one algorithm wants to trade
    # live. Saves creds-not-set warnings in paper-only deployments.
    any_live = any(a.params.mode == Mode.LIVE for a in ENABLED)
    client = runner.build_client() if any_live else None

    logger.info(
        "Profile: %s | algorithms: %s",
        config.PROFILE,
        ", ".join(
            f"{a.params.name}({a.params.mode.value})" for a in ENABLED
        ),
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
            target=_run_worker, args=(algo, client, stop_event),
            daemon=True, name=f"worker-{algo.params.name}",
        )
        for algo in ENABLED
    ]
    for w in workers:
        w.start()

    # One profile-level daily summary thread — aggregates all algorithms
    # into a single message sent to the profile's summary channel.
    _start_profile_summary(ENABLED, client, stop_event)

    # Heartbeat — periodic liveness ping to the summary channel.
    summary_webhook = config.resolve_summary_webhook(config.PROFILE)
    if summary_webhook and config.HEARTBEAT_INTERVAL_HOURS > 0:
        algo_infos = [
            (a.params.name, a.params.mode == Mode.PAPER or client is None)
            for a in ENABLED
        ]
        notifier.start_heartbeat(
            algo_infos, summary_webhook, stop_event,
            profile=config.PROFILE,
            interval_hours=config.HEARTBEAT_INTERVAL_HOURS,
        )

    # Weekly signal performance digest — posts every Sunday to the same channel.
    summary_webhook = config.resolve_summary_webhook(config.PROFILE)
    if summary_webhook:
        algo_infos = [
            (a.params.name, a.params.mode == Mode.PAPER or client is None)
            for a in ENABLED
        ]
        notifier.start_weekly_digest(
            algo_infos, summary_webhook, stop_event, profile=config.PROFILE
        )


    # Block the main thread until shutdown is signaled.
    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    finally:
        for w in workers:
            w.join(timeout=30)


if __name__ == "__main__":
    main()
