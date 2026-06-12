"""
Run-provenance + report/params CLI smoke tests.
"""

import json
import os
import subprocess
import sys

from bot.algorithm import Mode


def test_record_run_writes_resolved_params(fresh_db):
    from algorithms.copy_trade import CopyTradeParams
    from bot import runs

    p = CopyTradeParams(name="ct_test", mode=Mode.PAPER, target_address="0xabc",
                        tier1_size=5.0)
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
    from algorithms.copy_trade import CopyTradeParams
    from bot import db as dbmod, runs

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbmod, "get", boom)
    runs.record_run(CopyTradeParams(target_address="0xabc"), profile="x")  # no raise


def _run_module(mod, *args, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", mod, *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_params_cli_lists_types():
    out = _run_module("bot.params")
    assert out.returncode == 0, out.stderr
    assert "copy_trade" in out.stdout
    assert "insider_flow" in out.stdout


def test_params_cli_shows_schema_with_docs():
    out = _run_module("bot.params", "copy_trade")
    assert out.returncode == 0, out.stderr
    assert "tier1_min" in out.stdout
    assert "high-conviction" in out.stdout       # doc text surfaced
    assert "name" not in out.stdout.splitlines()[0]  # block-level keys hidden


def test_params_cli_effective_marks_overrides():
    out = _run_module("bot.params", "--effective",
                      env_extra={"PROFILE": "prod"})
    assert out.returncode == 0, out.stderr
    assert "copy_trade" in out.stdout
    assert "target_address" in out.stdout


def test_params_cli_effective_requires_profile():
    """No implicit profile — --effective without PROFILE must fail clearly."""
    env = {k: v for k, v in os.environ.items() if k != "PROFILE"}
    out = subprocess.run(
        [sys.executable, "-m", "bot.params", "--effective"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert out.returncode != 0
    assert "PROFILE is not set" in out.stderr


def test_report_cli_runs_on_fresh_db(tmp_path, monkeypatch):
    out = subprocess.run(
        [sys.executable, "-m", "bot.report"],
        capture_output=True, text=True, timeout=60,
        env={"DB_PATH": str(tmp_path / "r.db"), "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr
