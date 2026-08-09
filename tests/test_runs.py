"""
Run-provenance + report CLI smoke tests.
"""

import json
import os
import subprocess
import sys

from bot.domain.params import Mode


def test_record_run_writes_resolved_params(fresh_db):
    from bot import runs
    from tests.conftest import copy_trade_params

    p = copy_trade_params(name="ct_test", mode=Mode.PAPER,
                          target_address="0xabc", tier1_size=5.0)
    runs.record_run(p, profile="testprof")

    row = fresh_db.get().execute(
        "SELECT algo, mode, profile, params_json FROM runs"
    ).fetchone()
    assert row["algo"] == "ct_test"
    assert row["mode"] == "paper"
    assert row["profile"] == "testprof"
    params = json.loads(row["params_json"])
    assert params["tier1_size"] == 5.0
    assert params["target_address"] == "0xabc"


def test_record_run_never_raises(monkeypatch):
    """Provenance is best-effort — a DB failure must not kill the worker."""
    from bot import db as dbmod, runs
    from tests.conftest import copy_trade_params

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbmod, "get", boom)
    runs.record_run(copy_trade_params(target_address="0xabc"), profile="x")  # no raise


def _run_module(mod, *args, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", mod, *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_report_cli_runs_on_fresh_db(tmp_path, monkeypatch):
    out = subprocess.run(
        [sys.executable, "-m", "bot.report"],
        capture_output=True, text=True, timeout=60,
        env={"DB_PATH": str(tmp_path / "r.db"), "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr
