"""
Run-provenance tests.
"""

import json

from bot.domain.mode import Mode


def test_record_run_writes_resolved_params(fresh_db):
    from bot.storage import runs
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
    from bot.storage import db as dbmod, runs
    from tests.conftest import copy_trade_params

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbmod, "get", boom)
    runs.record_run(copy_trade_params(target_address="0xabc"), profile="x")  # no raise
