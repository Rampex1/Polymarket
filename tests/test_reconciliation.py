"""
Reconciliation tests — bot DB vs on-chain comparisons.

Stubs the HTTP boundary (`fetcher.fetch_user_positions`). Everything else
runs the real PositionTracker + DB.
"""


from tests.conftest import make_trade

from bot import reconciliation


def test_paper_mode_is_skipped(tracker):
    """Paper has nothing to reconcile against."""
    summary = reconciliation.reconcile_positions(
        tracker, funder_address="0xabc", algo_name="test", paper=True,
    )
    assert summary["skipped"] is True
    assert summary["agreed"] == []


def test_missing_funder_address_is_skipped(tracker):
    summary = reconciliation.reconcile_positions(
        tracker, funder_address="", algo_name="test", paper=False,
    )
    assert summary["skipped"] is True


def test_no_positions_either_side(tracker, monkeypatch):
    from bot import fetcher
    monkeypatch.setattr(fetcher, "fetch_user_positions", lambda *a, **kw: [])
    summary = reconciliation.reconcile_positions(
        tracker, funder_address="0xabc", algo_name="test", paper=False,
    )
    assert summary["agreed"] == []
    assert summary["stale"] == []
    assert summary["ghost"] == []
    assert summary["divergent"] == []


def test_agreed_position(tracker, monkeypatch):
    """Both sides hold the same shares → counts as agreed, no warning."""
    from bot import fetcher

    seed = make_trade(action="BUY", price=0.5)
    tracker.record_buy(seed, spent_usdc=3.0, shares=6.0, fill_price=0.5, paper=False)

    monkeypatch.setattr(
        fetcher, "fetch_user_positions",
        lambda *a, **kw: [{"conditionId": "m1", "size": 6.0, "value": 3.0, "asset": "a1"}],
    )
    summary = reconciliation.reconcile_positions(
        tracker, funder_address="0xabc", algo_name="test", paper=False,
    )
    assert summary["agreed"] == ["m1"]
    assert summary["stale"] == []
    assert summary["ghost"] == []
    assert summary["divergent"] == []


def test_stale_position_warns(tracker, monkeypatch, caplog):
    """DB has it, on-chain doesn't → STALE warning."""
    import logging
    from bot import fetcher

    seed = make_trade(action="BUY", price=0.5)
    tracker.record_buy(seed, spent_usdc=3.0, shares=6.0, fill_price=0.5, paper=False)

    monkeypatch.setattr(fetcher, "fetch_user_positions", lambda *a, **kw: [])
    with caplog.at_level(logging.WARNING):
        summary = reconciliation.reconcile_positions(
            tracker, funder_address="0xabc", algo_name="test", paper=False,
        )
    assert summary["stale"] == ["m1"]
    assert any("STALE" in r.message for r in caplog.records)


def test_ghost_position_warns(tracker, monkeypatch, caplog):
    """On-chain has it, DB doesn't → GHOST warning."""
    import logging
    from bot import fetcher

    monkeypatch.setattr(
        fetcher, "fetch_user_positions",
        lambda *a, **kw: [{"conditionId": "m1", "size": 6.0, "value": 3.0, "asset": "a1"}],
    )
    with caplog.at_level(logging.WARNING):
        summary = reconciliation.reconcile_positions(
            tracker, funder_address="0xabc", algo_name="test", paper=False,
        )
    assert summary["ghost"] == ["m1"]
    assert any("GHOST" in r.message for r in caplog.records)


def test_ghost_below_usd_epsilon_ignored(tracker, monkeypatch, caplog):
    """A dust on-chain position (value < $0.10) doesn't warn."""
    import logging
    from bot import fetcher

    monkeypatch.setattr(
        fetcher, "fetch_user_positions",
        lambda *a, **kw: [{"conditionId": "m1", "size": 0.05, "value": 0.03, "asset": "a1"}],
    )
    with caplog.at_level(logging.WARNING):
        summary = reconciliation.reconcile_positions(
            tracker, funder_address="0xabc", algo_name="test", paper=False,
        )
    assert summary["ghost"] == []
    assert not any("GHOST" in r.message for r in caplog.records)


def test_divergent_shares_warn(tracker, monkeypatch, caplog):
    """Both sides have it but shares disagree → DIVERGENT warning."""
    import logging
    from bot import fetcher

    seed = make_trade(action="BUY", price=0.5)
    tracker.record_buy(seed, spent_usdc=3.0, shares=6.0, fill_price=0.5, paper=False)

    monkeypatch.setattr(
        fetcher, "fetch_user_positions",
        lambda *a, **kw: [{"conditionId": "m1", "size": 4.0, "value": 2.0, "asset": "a1"}],
    )
    with caplog.at_level(logging.WARNING):
        summary = reconciliation.reconcile_positions(
            tracker, funder_address="0xabc", algo_name="test", paper=False,
        )
    assert summary["divergent"] == ["m1"]
    assert any("DIVERGENT" in r.message for r in caplog.records)


def test_divergent_within_epsilon_does_not_warn(tracker, monkeypatch, caplog):
    """Sub-epsilon difference is rounding noise, not real divergence."""
    import logging
    from bot import fetcher

    seed = make_trade(action="BUY", price=0.5)
    tracker.record_buy(seed, spent_usdc=3.0, shares=6.0, fill_price=0.5, paper=False)

    monkeypatch.setattr(
        fetcher, "fetch_user_positions",
        lambda *a, **kw: [{"conditionId": "m1", "size": 6.001, "value": 3.0, "asset": "a1"}],
    )
    with caplog.at_level(logging.WARNING):
        summary = reconciliation.reconcile_positions(
            tracker, funder_address="0xabc", algo_name="test", paper=False,
        )
    assert summary["agreed"] == ["m1"]
    assert summary["divergent"] == []


def test_both_sides_summed_at_market_level(tracker, monkeypatch):
    """If the wallet holds both YES and NO of one market, on-chain rows
    are summed before comparing — the DB keys by market_id, not asset_id."""
    from bot import fetcher

    seed = make_trade(action="BUY", price=0.5)
    tracker.record_buy(seed, spent_usdc=4.0, shares=8.0, fill_price=0.5, paper=False)

    monkeypatch.setattr(
        fetcher, "fetch_user_positions",
        lambda *a, **kw: [
            {"conditionId": "m1", "size": 5.0, "value": 2.5, "asset": "yes"},
            {"conditionId": "m1", "size": 3.0, "value": 1.5, "asset": "no"},
        ],
    )
    summary = reconciliation.reconcile_positions(
        tracker, funder_address="0xabc", algo_name="test", paper=False,
    )
    # 5 + 3 = 8 shares total, matches DB.
    assert summary["agreed"] == ["m1"]
    assert summary["divergent"] == []
