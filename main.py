#!/usr/bin/env python3
"""
Architecture
------------
Each algorithm in `algorithms.ENABLED` (selected by the `PROFILE` env var)
runs as a fully independent worker:

  * Own `Ledger(algo=...)` — DB rows partitioned by algo.
  * Own `RiskManager(ledger, params)` — caps come from algo params.
  * Own polling cadence (`params.poll_interval_seconds`).
  * Own paper bankroll (per-algo row in `paper_account`).

Crashes in one algorithm do not affect the others.
"""

import logging
import signal
import sys
import threading

from bot import config, reconciliation
from bot.execution import runner
from bot.storage import db, runs
from bot.domain.algorithm import Algorithm
from bot.domain.params import Mode
from bot.discord import alerts, heartbeat
from bot.logs.setup import setup_logging
from bot.storage.ledger import Ledger
from bot.execution.risk import RiskManager
from bot.profile_loader import ProfileError

try:
    from algorithms import ENABLED
except ProfileError as e:
    raise SystemExit(f"error: {e}") from None


# Reconcilier
RECONCILE_EVERY_N_POLLS = 30
CRASH_ALERT_AFTER_N_ERRORS = 5


# Logging
setup_logging(config.PROFILE)
logger = logging.getLogger(__name__)


def _run_worker(
    algo: Algorithm,
    client,
    stop_event: threading.Event,
) -> None:
    """One algorithm's lifecycle: setup → poll loop → shutdown."""
    name = algo.params.name
    paper = algo.params.mode == Mode.PAPER

    try:
        ledger = Ledger(algo=name)
        if paper:
            ledger.init_paper_balance(algo.params.paper_starting_balance)
        runs.record_run(algo.params, config.PROFILE)
        risk = RiskManager(ledger, algo.params)
        algo.setup(ledger)

        display_name = algo.display_name
        webhook_url = algo.params.webhook_url

        alerts.on_startup(
            "PAPER" if paper else "LIVE",
            ledger.total_exposure_usdc(paper=paper),
            algo_name=display_name,
            webhook_url=webhook_url,
        )
        logger.info(
            "[%s] Started (%s) — poll every %ds | risk: per-position $%.2f, "
            "total $%.2f, daily loss $%.2f | slippage %.0f%% | order=%s",
            name,
            "PAPER" if paper else "LIVE",
            algo.params.poll_interval_seconds,
            algo.params.max_position_size_usdc,
            algo.params.max_total_exposure_usdc,
            algo.params.daily_loss_limit_usdc,
            algo.params.max_slippage * 100,
            algo.params.order_type.upper(),
        )
        ledger.print_summary(paper=paper)

        # Startup reconciliation — surfaces ghost positions or stale DB rows
        # before we start acting. Live-only; paper has nothing to reconcile.
        reconciliation.reconcile_positions(
            ledger,
            config.POLY_FUNDER_ADDRESS,
            algo.display_name,
            paper,
        )

        poll_count = 0
        consecutive_errors = 0
        while not stop_event.is_set():
            try:
                for intent in algo.poll():
                    if stop_event.is_set():
                        break
                    runner.dispatch(intent, algo, ledger, risk, client, paper)
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                logger.exception("[%s] poll/dispatch raised, continuing", name)
                if consecutive_errors % CRASH_ALERT_AFTER_N_ERRORS == 0:
                    alerts.on_worker_unstable(
                        display_name,
                        consecutive_errors,
                        exc,
                        webhook_url=webhook_url,
                    )

            # Periodic reconciliation. Failures inside reconcile_positions
            # only log; they never abort the worker.
            poll_count += 1
            if not paper and poll_count % RECONCILE_EVERY_N_POLLS == 0:
                try:
                    reconciliation.reconcile_positions(
                        ledger,
                        config.POLY_FUNDER_ADDRESS,
                        algo.display_name,
                        paper,
                    )
                except Exception:
                    logger.exception("[%s] reconciliation raised, continuing", name)

            stop_event.wait(algo.params.poll_interval_seconds)
    finally:
        try:
            ledger = Ledger(algo=name)
            ledger.print_summary(paper=paper)
        except Exception:
            pass
        alerts.on_shutdown(
            algo_name=algo.display_name, webhook_url=algo.params.webhook_url
        )
        logger.info("[%s] Stopped.", name)


def _warn_orphaned_algos() -> None:
    """Warn if the DB holds open positions under names absent from the
    profile — catches accidental renames in config/<profile>.toml, which
    would silently orphan a paper bankroll and its position history."""
    try:
        enabled_names = {a.params.name for a in ENABLED}
        rows = (
            db.get()
            .execute("SELECT DISTINCT algo FROM positions WHERE shares > 0")
            .fetchall()
        )
        for (orphan,) in rows:
            if orphan not in enabled_names:
                logger.warning(
                    "DB has open positions under algo '%s', which is not in "
                    "profile '%s' — renamed or removed in config? Its "
                    "positions and paper bankroll are now unmanaged.",
                    orphan,
                    config.PROFILE,
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

    # Declaring live and quietly trading paper is the worst outcome: the
    # operator believes real money is at work. Refuse to start instead.
    if any_live and client is None:
        live = ", ".join(a.params.name for a in ENABLED if a.params.mode == Mode.LIVE)
        raise SystemExit(
            f'error: {live} declared mode="live" but no CLOB client could be '
            "built — see the error above (POLY_PRIVATE_KEY and "
            "POLY_FUNDER_ADDRESS are both required). Fix the creds, or set "
            'mode = "paper" if that was the intent.'
        )

    logger.info(
        "Profile: %s | algorithms: %s",
        config.PROFILE,
        ", ".join(f"{a.params.name}({a.params.mode.value})" for a in ENABLED),
    )

    # Single Event coordinates shutdown for every worker and shared thread.
    stop_event = threading.Event()

    def _shutdown(signum, frame):
        if stop_event.is_set():
            return  # second Ctrl-C: let default handler kill us
        logger.info("Shutdown signal received, finishing current cycles...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    workers = [
        threading.Thread(
            target=_run_worker,
            args=(algo, client, stop_event),
            daemon=True,
            name=f"worker-{algo.params.name}",
        )
        for algo in ENABLED
    ]
    for w in workers:
        w.start()

    # Heartbeat — the only scheduled message: proof the process is alive.
    summary_webhook = config.resolve_summary_webhook(config.PROFILE)
    if summary_webhook and config.HEARTBEAT_INTERVAL_HOURS > 0:
        heartbeat.start(
            summary_webhook,
            stop_event,
            profile=config.PROFILE,
            interval_hours=config.HEARTBEAT_INTERVAL_HOURS,
            n_algos=len(ENABLED),
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
