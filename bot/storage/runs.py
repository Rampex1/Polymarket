"""
Run provenance — one `runs` row per worker boot.

Records the fully-resolved params (JSON), profile, and git sha, so the
config that produced a stretch of results is recoverable after the fact —
comparing two paper variants is only valid if neither changed mid-experiment.

The rows are written but not yet joined against: `bot.report` reads only
the latest run, to print one `last boot:` line. Attributing trades and
signals to the run that produced them is unimplemented.
"""

import dataclasses
import json
import logging
import subprocess
import time

from . import db

logger = logging.getLogger(__name__)


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _params_json(params) -> str:
    d = dataclasses.asdict(params) if dataclasses.is_dataclass(params) else dict(vars(params))
    return json.dumps(d, default=str, sort_keys=True)


def record_run(params, profile: str) -> None:
    """Insert a runs row. Never raises — provenance must not kill a worker."""
    try:
        mode = getattr(params.mode, "value", str(params.mode))
        conn = db.get()
        conn.execute(
            "INSERT INTO runs (algo, mode, profile, git_sha, params_json, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (params.name, mode, profile, _git_sha(), _params_json(params), int(time.time())),
        )
        conn.commit()
    except Exception:
        logger.exception("[%s] failed to record run row (continuing)", getattr(params, "name", "?"))
