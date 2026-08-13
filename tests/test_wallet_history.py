"""Reconstructing resolved bets from /activity + /positions. Pure — no I/O."""

from algorithms.copy_trade.history import resolved_bets_from


def buy(cid="m1", idx=0, shares=100.0, usdc=40.0):
    return {"type": "TRADE", "side": "BUY", "conditionId": cid, "outcomeIndex": idx,
            "size": shares, "usdcSize": usdc, "price": usdc / shares, "timestamp": 1}


def redeem(cid="m1", idx=0, usdc=100.0, ts=500):
    return {"type": "REDEEM", "conditionId": cid, "outcomeIndex": idx,
            "usdcSize": usdc, "size": usdc, "timestamp": ts}


def settled(cid="m1", idx=0, entry=0.30, cur=0.0, end="2026-07-20"):
    return {"conditionId": cid, "outcomeIndex": idx, "redeemable": True,
            "avgPrice": entry, "curPrice": cur, "endDate": end}


def test_a_redeemed_bet_is_a_win_at_its_fee_inclusive_basis():
    (bet,) = resolved_bets_from("0xA", [buy(usdc=44.0), redeem()], [])
    assert bet.outcome == 1.0
    assert bet.entry_price == 0.44        # usdcSize/shares, not the quoted price
    assert bet.edge == 0.56
    assert bet.wallet == "0xa"


def test_an_unredeemed_settled_bet_is_a_loss():
    (bet,) = resolved_bets_from("0xA", [buy()], [settled()])
    assert (bet.outcome, bet.entry_price) == (0.0, 0.40)


def test_an_unclaimed_winner_still_reads_as_a_win():
    (bet,) = resolved_bets_from("0xA", [buy()], [settled(cur=1.0)])
    assert bet.outcome == 1.0


def test_both_outcomes_are_anchored_to_a_buy_so_neither_source_dominates():
    # Read as two independent sources these skew hard in opposite directions:
    # /positions keeps only unclaimed losers, REDEEM rows only winners.
    activity = [buy(cid="win"), redeem(cid="win"), buy(cid="lose")]
    bets = resolved_bets_from("0xA", activity, [settled(cid="lose")])
    assert sorted(b.outcome for b in bets) == [0.0, 1.0]


def test_a_bet_with_no_verdict_yet_is_not_scored():
    # Still trading, or sold off before it resolved. An exit is not a verdict.
    assert resolved_bets_from("0xA", [buy()], []) == []


def test_a_verdict_with_no_buy_in_the_window_is_not_scored():
    # No entry price to score it against. A fabricated basis is worse than a gap.
    assert resolved_bets_from("0xA", [redeem()], []) == []
    assert resolved_bets_from("0xA", [], [settled()]) == []


def test_opposite_sides_of_one_market_are_separate_bets():
    activity = [buy(idx=0), redeem(idx=0), buy(idx=1)]
    bets = resolved_bets_from("0xA", activity, [settled(idx=1)])
    assert sorted(b.outcome for b in bets) == [0.0, 1.0]


def test_partial_redemptions_count_once():
    bets = resolved_bets_from("0xA", [buy(), redeem(usdc=60), redeem(usdc=40, ts=600)], [])
    assert len(bets) == 1


def test_undated_and_nonsense_rows_are_skipped():
    assert resolved_bets_from("0xA", [buy()], [settled(end="")]) == []
    assert resolved_bets_from("0xA", [buy(usdc=0.0)], [settled()]) == []
    assert resolved_bets_from("0xA", [buy(usdc=100.0)], [settled()]) == []   # entry == 1.0


def test_a_failed_page_is_not_an_exhausted_history(monkeypatch):
    """A failed read must never be mistaken for the end of a wallet's record."""
    import scripts.rank_wallets as rw

    pages = {0: [buy()] * 500, 500: None}          # page two fails
    monkeypatch.setattr(rw.api, "fetch_activity",
                        lambda w, limit, offset: pages.get(offset, []))
    monkeypatch.setattr(rw.api, "fetch_user_positions", lambda w, limit, offset: [])
    monkeypatch.setattr(rw, "store_resolved_bets", lambda bets: len(list(bets)))

    _, truncated = rw.ingest("0xA", max_pages=4)
    assert truncated is True
