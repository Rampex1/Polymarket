"""Out-of-sample validation of wallet selection. Pure — no DB, no network."""

from algorithms.copy_trade.ranker import ResolvedBet, holdout_report

SPLIT = 1_000


def run(wallets, **kw):
    """wallets: {name: (train_outcome, test_outcome)} at a 0.40 entry."""
    bets = []
    for name, (early, late) in wallets.items():
        bets += [ResolvedBet(name, 0.40, early, SPLIT - 100 + i, 1.0) for i in range(40)]
        bets += [ResolvedBet(name, 0.40, late, SPLIT + i, 1.0) for i in range(40)]
    return holdout_report(bets, split_at=SPLIT, min_resolved_bets=10,
                          confidence_z=1.645, min_copyability_score=0.0, **kw)


def test_selection_that_generalises_shows_positive_lift():
    # "good" wins in both halves; the two others lose in both.
    r = run({"0xgood": (1.0, 1.0), "0xbad1": (0.0, 0.0), "0xbad2": (0.0, 0.0)}, top_n=1)
    assert r.per_wallet[0][0] == "0xgood"
    assert r.selected_edge == 0.60          # +1.00 outcome less 0.40 entry
    assert r.rest_edge == -0.40
    assert r.lift == 1.0


def test_a_fluke_that_reverses_is_exposed_not_hidden():
    # "fluke" tops the training half and then collapses — exactly the failure
    # an in-sample-only ranking cannot see.
    r = run({"0xfluke": (1.0, 0.0), "0xsteady": (0.0, 1.0)}, top_n=1)
    picked, _, delivered = r.per_wallet[0]
    assert picked == "0xfluke"
    assert delivered == -0.40               # what it actually did after selection
    assert r.lift < 0                       # ranking was worse than passing over


def test_no_lift_when_every_wallet_behaves_the_same():
    r = run({f"0x{i}": (1.0, 1.0) for i in range(4)}, top_n=2)
    assert r.lift == 0.0


def test_wallets_without_enough_holdout_history_are_not_judged():
    # A wallet with one bet after the split cannot be scored, and must not be
    # compared against wallets that can be.
    bets = [ResolvedBet("0xa", 0.4, 1.0, SPLIT - 100 + i, 1.0) for i in range(40)]
    bets += [ResolvedBet("0xa", 0.4, 1.0, SPLIT + i, 1.0) for i in range(40)]
    bets += [ResolvedBet("0xthin", 0.4, 1.0, SPLIT - 100 + i, 1.0) for i in range(40)]
    bets += [ResolvedBet("0xthin", 0.4, 0.0, SPLIT + 1, 1.0)]
    assert holdout_report(bets, split_at=SPLIT, min_resolved_bets=10,
                          confidence_z=1.645, min_copyability_score=0.0,
                          top_n=1, min_holdout_bets=20) is None    # only 0xa judgeable


def test_too_little_history_returns_none_rather_than_a_verdict():
    bets = [ResolvedBet("0xa", 0.4, 1.0, SPLIT - 1, 1.0) for _ in range(40)]
    assert holdout_report(bets, split_at=SPLIT, min_resolved_bets=10,
                          confidence_z=1.645, min_copyability_score=0.0,
                          top_n=1) is None


def _bet(w, entry, outcome, ts):
    return ResolvedBet(w, entry, outcome, ts, 1.0)


def test_sigma_filter_drops_near_certainty_harvesters():
    """A wallet only ever betting at ~0.99 has no forecast to copy."""
    from algorithms.copy_trade.ranker import rank_wallets

    # Spread-earner: 60 bets at 0.99, all won. Real edge, near-zero variance.
    mm = [_bet("0xmm", 0.99, 1.0, i) for i in range(60)]
    # Forecaster: 60 bets at 0.40, two thirds won. Same sign, real variance.
    fc = [_bet("0xfc", 0.40, 1.0 if i % 3 else 0.0, i) for i in range(60)]

    both = rank_wallets(mm + fc, 50, 1.645, 0.0)
    assert {s.wallet for s in both} == {"0xmm", "0xfc"}
    assert dict((s.wallet, s.edge_stdev) for s in both)["0xmm"] < 0.05

    filtered = rank_wallets(mm + fc, 50, 1.645, 0.0, min_edge_stdev=0.15)
    assert [s.wallet for s in filtered] == ["0xfc"]


def test_a_single_bet_has_no_measurable_spread():
    from algorithms.copy_trade.ranker import score_wallet

    assert score_wallet("0xa", [_bet("0xa", 0.5, 1.0, 1)], 1.645).edge_stdev == 0.0
