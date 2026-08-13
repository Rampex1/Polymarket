"""Reconstructing resolved bets from /activity + /positions. Pure — no I/O."""

from algorithms.copy_trade.history import resolved_bets_from


def buy(cid="m1", idx=0, shares=100.0, usdc=40.0):
    return {"type": "TRADE", "side": "BUY", "conditionId": cid, "outcomeIndex": idx,
            "size": shares, "usdcSize": usdc, "price": usdc / shares, "timestamp": 1}


def redeem(cid="m1", idx=0, usdc=100.0, ts=500):
    return {"type": "REDEEM", "conditionId": cid, "outcomeIndex": idx,
            "usdcSize": usdc, "size": usdc, "timestamp": ts}


def lost(entry=0.30, cur=0.0, end="2026-07-20"):
    return {"redeemable": True, "avgPrice": entry, "curPrice": cur, "endDate": end}


def test_redeemed_bet_is_a_win_priced_at_its_fee_inclusive_basis():
    (bet,) = resolved_bets_from("0xA", [buy(usdc=44.0), redeem()], [])
    assert bet.outcome == 1.0
    assert bet.entry_price == 0.44        # usdcSize/shares, not the quoted price
    assert bet.edge == 0.56
    assert bet.wallet == "0xa"


def test_unredeemed_resolved_position_is_a_loss():
    (bet,) = resolved_bets_from("0xA", [], [lost()])
    assert (bet.outcome, bet.entry_price) == (0.0, 0.30)


def test_the_two_sources_together_are_not_all_losses():
    # Either source alone is 100% skewed: winners get claimed and drop off
    # /positions, losers never appear in REDEEM rows.
    bets = resolved_bets_from("0xA", [buy(), redeem()], [lost(), lost(entry=0.6)])
    assert sorted(b.outcome for b in bets) == [0.0, 0.0, 1.0]


def test_a_sale_before_resolution_is_not_a_verdict():
    # Bought and never redeemed, nothing left on the books — they traded out.
    assert resolved_bets_from("0xA", [buy()], []) == []


def test_a_redeem_whose_buys_fell_off_the_page_window_is_dropped():
    # No entry price to score. A fabricated basis would be worse than a gap.
    assert resolved_bets_from("0xA", [redeem()], []) == []


def test_partial_redemptions_count_once():
    bets = resolved_bets_from("0xA", [buy(), redeem(usdc=60), redeem(usdc=40, ts=600)], [])
    assert len(bets) == 1


def test_an_unclaimed_winner_still_reads_as_a_win():
    (bet,) = resolved_bets_from("0xA", [], [lost(entry=0.30, cur=1.0)])
    assert bet.outcome == 1.0


def test_undated_and_nonsense_rows_are_skipped():
    rows = [lost(end=""), lost(entry=0.0), lost(entry=1.0)]
    assert resolved_bets_from("0xA", [], rows) == []


def test_a_capped_crawl_clamps_losses_to_the_window_the_wins_cover():
    # /positions keeps lifetime losers; a capped /activity crawl sees days.
    # Scoring one against the other invents a hugely negative edge.
    activity = [buy(), redeem(ts=1_000_000)]
    positions = [lost(end="2026-08-10"), lost(end="2026-01-01")]

    unclamped = resolved_bets_from("0xA", activity, positions)
    clamped = resolved_bets_from("0xA", activity, positions,
                                 since_ts=int(__import__("datetime").datetime(
                                     2026, 6, 1).timestamp()))

    assert sorted(b.outcome for b in unclamped) == [0.0, 0.0, 1.0]
    assert sorted(b.outcome for b in clamped) == [0.0, 1.0]
