"""
Signal feature-logging tests.

The `signals` table is the training-data store: one row per signal the runner
saw, with raw features captured at signal time and the outcome backfilled at
settlement. Real SQLite via the fresh_db fixture; recording must NEVER raise
into the dispatch path.
"""

import json


from bot.domain.intents import OpenIntent


def _intent(**overrides):
    base = dict(
        market_id="m1",
        asset_id="a1",
        usdc_amount=10.0,
        signal_price=0.20,
        question="Will X happen?",
        outcome="Yes",
        signal_id="sig1",
        reason="test",
        features={"odds": 0.20, "cash_usdc": 10_000.0, "wallet": "0xwhale"},
    )
    base.update(overrides)
    return OpenIntent(**base)


# ---------------------------------------------------------------------------
# record — raw capture
# ---------------------------------------------------------------------------


def test_record_writes_row_with_features_json(fresh_db):
    from bot import db, signals

    signals.record("algo_a", _intent(), paper=True, executed=True)

    row = db.get().execute(
        "SELECT * FROM signals WHERE signal_id='sig1' AND algo='algo_a'"
    ).fetchone()
    assert row is not None
    assert row["executed"] == 1
    assert row["paper"] == 1
    assert row["market_id"] == "m1"
    assert row["asset_id"] == "a1"
    assert row["signal_price"] == 0.20
    assert row["usdc_amount"] == 10.0
    assert row["outcome"] is None                       # unlabeled until settle
    assert json.loads(row["features"]) == {
        "odds": 0.20, "cash_usdc": 10_000.0, "wallet": "0xwhale",
    }


def test_record_skip_reason_for_unexecuted(fresh_db):
    from bot import db, signals

    signals.record(
        "algo_a", _intent(), paper=True, executed=False, skip_reason="risk: capped",
    )
    row = db.get().execute("SELECT executed, skip_reason FROM signals").fetchone()
    assert row["executed"] == 0
    assert row["skip_reason"] == "risk: capped"


def test_record_upserts_on_same_signal_and_algo(fresh_db):
    from bot import db, signals

    signals.record("algo_a", _intent(), paper=True, executed=False,
                   skip_reason="slippage")
    signals.record("algo_a", _intent(), paper=True, executed=True)

    rows = db.get().execute("SELECT executed, skip_reason FROM signals").fetchall()
    assert len(rows) == 1
    assert rows[0]["executed"] == 1
    assert rows[0]["skip_reason"] is None


def test_record_same_signal_id_different_algo_is_distinct(fresh_db):
    from bot import db, signals

    signals.record("algo_a", _intent(), paper=True, executed=True)
    signals.record("algo_b", _intent(), paper=True, executed=False,
                   skip_reason="risk: x")
    rows = db.get().execute("SELECT algo FROM signals ORDER BY algo").fetchall()
    assert [r["algo"] for r in rows] == ["algo_a", "algo_b"]


def test_record_without_signal_id_is_dropped(fresh_db):
    """No signal_id → no dedupe key → don't store junk."""
    from bot import db, signals

    signals.record("algo_a", _intent(signal_id=""), paper=True, executed=True)
    assert db.get().execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


def test_record_never_raises_on_unserializable_features(fresh_db):
    """Feature capture must not be able to break dispatch — coerce or drop."""
    from bot import db, signals

    signals.record(
        "algo_a", _intent(features={"weird": object()}), paper=True, executed=True,
    )
    row = db.get().execute("SELECT features FROM signals").fetchone()
    assert row is not None
    json.loads(row["features"])                          # still valid JSON


# ---------------------------------------------------------------------------
# label_outcomes — settle-time backfill
# ---------------------------------------------------------------------------


def test_label_outcomes_sets_outcome_on_unlabeled_rows(fresh_db):
    from bot import db, signals

    signals.record("algo_a", _intent(), paper=True, executed=True)
    n = signals.label_outcomes("algo_a", "m1", close_price=1.0,
                               pnl_usdc=40.0, paper=True)
    assert n == 1

    row = db.get().execute("SELECT outcome, pnl_usdc, outcome_ts FROM signals").fetchone()
    assert row["outcome"] == 1.0
    assert row["pnl_usdc"] == 40.0
    assert row["outcome_ts"] is not None


def test_label_outcomes_is_idempotent(fresh_db):
    """A second settle (or a re-run) must not overwrite the first label."""
    from bot import db, signals

    signals.record("algo_a", _intent(), paper=True, executed=True)
    signals.label_outcomes("algo_a", "m1", close_price=1.0, pnl_usdc=40.0, paper=True)
    n = signals.label_outcomes("algo_a", "m1", close_price=0.0, pnl_usdc=-10.0, paper=True)
    assert n == 0

    row = db.get().execute("SELECT outcome, pnl_usdc FROM signals").fetchone()
    assert row["outcome"] == 1.0
    assert row["pnl_usdc"] == 40.0


def test_label_outcomes_scoped_to_algo_market_and_paper(fresh_db):
    from bot import db, signals

    signals.record("algo_a", _intent(signal_id="s1"), paper=True, executed=True)
    signals.record("algo_a", _intent(signal_id="s2", market_id="m2"), paper=True,
                   executed=True)
    signals.record("algo_b", _intent(signal_id="s3"), paper=True, executed=True)

    signals.label_outcomes("algo_a", "m1", close_price=1.0, pnl_usdc=5.0, paper=True)

    labeled = {
        (r["algo"], r["signal_id"]): r["outcome"]
        for r in db.get().execute("SELECT algo, signal_id, outcome FROM signals")
    }
    assert labeled[("algo_a", "s1")] == 1.0
    assert labeled[("algo_a", "s2")] is None
    assert labeled[("algo_b", "s3")] is None


def test_unlabeled_market_ids_lists_pending_markets(fresh_db):
    from bot import signals

    signals.record("algo_a", _intent(signal_id="s1", market_id="m1"), paper=True,
                   executed=False, skip_reason="risk: capped")
    signals.record("algo_a", _intent(signal_id="s2", market_id="m2"), paper=True,
                   executed=True)
    signals.label_outcomes("algo_a", "m2", close_price=0.0, pnl_usdc=-1.0, paper=True)

    assert signals.unlabeled_market_ids("algo_a", paper=True) == ["m1"]
