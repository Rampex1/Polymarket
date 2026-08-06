"""Logging setup — console plus a daily-rotating file under logs/.

tmux scrollback dies with the pane, so a crash investigated after a restart
has no history. The file sink is what survives.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = "logs"
KEEP_DAYS = 14

_CONSOLE_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(profile: str = "", level: int = logging.INFO) -> None:
    """Configure the root logger. Idempotent — a second call is a no-op."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)

    console = logging.StreamHandler()
    # Console keeps the time-only stamp; files get the date too, since a
    # rotated file outlives the day you read it in.
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{profile or 'bot'}.log")
    rotating = TimedRotatingFileHandler(
        path, when="midnight", backupCount=KEEP_DAYS
    )
    rotating.setFormatter(logging.Formatter(_CONSOLE_FMT))
    root.addHandler(rotating)
