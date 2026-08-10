"""
heartbeat.py

The one scheduled message in the bot: a periodic "still running" ping.

Deliberately carries no portfolio data — exposure, P&L, and positions are
answered on demand by the slash commands. This exists only so silence in
the channel means something is wrong.
"""

import logging
import threading
from datetime import datetime

from .. import config
from .messages import escape as _esc
from .webhook import send

logger = logging.getLogger(__name__)


def ping(webhook_url: str, profile: str = "", n_algos: int = 0) -> None:
    """Post one liveness ping. Nothing but 'this process is alive'."""
    if not webhook_url:
        return
    now_str = datetime.now(tz=config.TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
    profile_label = _esc(profile) if profile else "all"
    algo_word = "algorithm" if n_algos == 1 else "algorithms"
    send(
        f"💓 **Alive · {profile_label} · {now_str}** — {n_algos} {algo_word} running",
        webhook_url=webhook_url,
    )


def start(
    webhook_url: str,
    stop_event: threading.Event | None = None,
    profile: str = "",
    interval_hours: float = 6.0,
    n_algos: int = 0,
) -> None:
    """Ping every `interval_hours` hours. `0` disables.

    The first ping fires after one full interval, not on boot — the startup
    alert already says the process came up.
    """
    if not webhook_url or interval_hours <= 0:
        return

    interval_s = interval_hours * 3600

    def _loop() -> None:
        while True:
            if stop_event is not None:
                if stop_event.wait(interval_s):
                    return
            else:
                threading.Event().wait(interval_s)
            try:
                ping(webhook_url, profile, n_algos)
            except Exception:
                logger.exception("Heartbeat raised, continuing")

    t = threading.Thread(target=_loop, daemon=True, name="heartbeat")
    t.start()
    logger.info(
        "Heartbeat thread started (profile=%s, interval=%.1fh).",
        profile or "?", interval_hours,
    )
