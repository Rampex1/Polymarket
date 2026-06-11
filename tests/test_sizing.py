"""
Sizing math tests — Kelly criterion forward + inverse for binary markets.

Pure functions, no I/O. For a binary market at share price q (win pays 1):
    f* = (p − q) / (1 − q)          # optimal bankroll fraction given belief p
    p  = q + f·(1 − q)              # belief implied by an observed fraction f
"""

import pytest


def test_kelly_fraction_known_value():
    from bot.sizing import kelly_fraction

    # Believe 60% on a 20-cent market → bet half the bankroll.
    assert kelly_fraction(p=0.6, q=0.2) == pytest.approx(0.5)


def test_kelly_fraction_no_edge_is_zero():
    from bot.sizing import kelly_fraction

    assert kelly_fraction(p=0.2, q=0.2) == 0.0
    assert kelly_fraction(p=0.1, q=0.2) == 0.0      # negative edge → don't bet


def test_kelly_fraction_certainty_is_full_bankroll():
    from bot.sizing import kelly_fraction

    assert kelly_fraction(p=1.0, q=0.2) == pytest.approx(1.0)


def test_kelly_fraction_rejects_degenerate_prices():
    from bot.sizing import kelly_fraction

    for bad_q in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            kelly_fraction(p=0.5, q=bad_q)


def test_implied_belief_known_value():
    from bot.sizing import implied_belief

    # Half the bankroll on a 20-cent market implies p = 0.2 + 0.5·0.8 = 0.6.
    assert implied_belief(bet_fraction=0.5, q=0.2) == pytest.approx(0.6)


def test_implied_belief_roundtrips_kelly_fraction():
    from bot.sizing import implied_belief, kelly_fraction

    for p, q in [(0.6, 0.2), (0.9, 0.5), (0.35, 0.1), (0.99, 0.97)]:
        assert implied_belief(kelly_fraction(p, q), q) == pytest.approx(p)


def test_implied_belief_clamps_to_probability_range():
    from bot.sizing import implied_belief

    # An over-Kelly bettor (fraction > 1) can't imply belief above certainty.
    assert implied_belief(bet_fraction=1.5, q=0.2) == 1.0
    # A zero bet implies no more than the market price (lower bound).
    assert implied_belief(bet_fraction=0.0, q=0.2) == pytest.approx(0.2)


def test_edge_is_belief_minus_price():
    from bot.sizing import edge

    assert edge(p=0.6, q=0.2) == pytest.approx(0.4)
    assert edge(p=0.1, q=0.2) == pytest.approx(-0.1)


def test_stake_fractional_kelly_and_cap():
    from bot.sizing import stake

    # Full Kelly would be 0.5 × 1000 = 500; quarter-Kelly → 125.
    assert stake(p=0.6, q=0.2, bankroll=1000.0, kelly_scale=0.25) == pytest.approx(125.0)
    # Absolute cap binds when smaller than the Kelly stake.
    assert stake(p=0.6, q=0.2, bankroll=1000.0, kelly_scale=0.25, cap=50.0) == 50.0


def test_stake_never_negative_and_zero_bankroll_is_zero():
    from bot.sizing import stake

    assert stake(p=0.1, q=0.2, bankroll=1000.0) == 0.0     # negative edge
    assert stake(p=0.6, q=0.2, bankroll=0.0) == 0.0
