"""Logging setup — console plus per-level rotating files under logs/<profile>/.

tmux scrollback dies with the pane, so a crash investigated after a restart
has no history. The file sinks are what survive.

Files are cumulative: error.log is the short list of what broke, info.log
the normal narrative, debug.log the full story including third-party HTTP
chatter. The same ERROR record lands in all three.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = "logs"
KEEP_DAYS = 14

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Each file takes its level and everything above it.
_FILES = (
    (logging.DEBUG, "debug.log"),
    (logging.INFO, "info.log"),
    (logging.ERROR, "error.log"),
)

# These emit a line per HTTP retry, frame, and heartbeat at DEBUG. Capped so
# debug.log stays mostly our code.
_NOISY = ("urllib3", "requests", "py_clob_client_v2", "discord", "websockets")


def setup_logging(profile: str = "", level: int = logging.INFO) -> None:
    """Configure the root logger. `level` sets the console only — files are
    always DEBUG/INFO/ERROR. Idempotent: a second call is a no-op.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    # Root passes everything; each handler decides what it keeps.
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_FMT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    log_dir = os.path.join(LOG_DIR, profile or "bot")
    os.makedirs(log_dir, exist_ok=True)
    for handler_level, filename in _FILES:
        handler = TimedRotatingFileHandler(
            os.path.join(log_dir, filename),
            when="midnight",
            backupCount=KEEP_DAYS,
        )
        handler.setLevel(handler_level)
        handler.setFormatter(logging.Formatter(_FMT))
        root.addHandler(handler)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.INFO)
