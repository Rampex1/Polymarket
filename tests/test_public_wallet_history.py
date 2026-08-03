from algorithms.copy_trade.public_history import (
    collect_closed_position_bets,
    collect_resolved_bets,
    resolved_bet_from_closed_position,
    resolved_bet_from_trade,
)


def _market(final=True):
    return {
        "resolved": final,
        "closed": final,
        "clobTokenIds": '["yes", "no"]',
        "outcomePrices": '["1", "0"]',
        "closedTime": "2026-08-01T12:00:00Z",
    }


def test_normalizes_only_final_buy_trades():
    bet = resolved_bet_from_trade(
        "0xWallet", {"asset": "yes", "price": 0.42, "timestamp": 1}, _market(),
    )

    assert bet is not None
    assert bet.wallet == "0xwallet"
    assert bet.entry_price == 0.42
    assert bet.outcome == 1.0
    assert bet.resolved_at == 1_785_585_600


def test_rejects_trade_when_market_is_not_final():
    assert resolved_bet_from_trade(
        "0xwallet", {"asset": "yes", "price": 0.42, "timestamp": 1}, _market(False),
    ) is None


def test_collection_caches_market_lookup_and_skips_bad_rows():
    calls = []

    def trades(wallet, pages, history_end):
        assert history_end == 123
        return [
            {"conditionId": "m1", "asset": "yes", "price": 0.4, "timestamp": 1},
            {"conditionId": "m1", "asset": "no", "price": 0.6, "timestamp": 1},
            {"conditionId": "m2", "asset": "yes", "price": 1.0, "timestamp": 1},
        ]

    def market(market_id):
        calls.append(market_id)
        return _market()

    bets = collect_resolved_bets(["0xa"], 1, trades, market, history_end=123)

    assert [(bet.entry_price, bet.outcome) for bet in bets] == [(0.4, 1.0), (0.6, 0.0)]
    assert calls == ["m1", "m2"]


def test_closed_position_fallback_requires_binary_old_market():
    row = {"avgPrice": 0.4, "curPrice": 1, "endDate": "2026-01-01T00:00:00Z", "timestamp": 8}
    assert resolved_bet_from_closed_position("0xa", row, 100, 2_000_000_000) is not None
    assert resolved_bet_from_closed_position("0xa", {**row, "curPrice": 0.999}, 100, 2_000_000_000) is None


def test_closed_position_collection_is_injectable():
    def positions(wallet, pages):
        return [{"avgPrice": 0.5, "curPrice": 0, "endDate": "2026-01-01T00:00:00Z", "timestamp": 9}]

    bets = collect_closed_position_bets(["0xa"], 1, 100, 2_000_000_000, positions)
    assert [(bet.wallet, bet.outcome) for bet in bets] == [("0xa", 0.0)]
