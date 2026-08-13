"""Calibration bucketing. Pure — no DB, no network."""

from algorithms.resolution_carry.calibration import calibrate


def obs(price, hours, outcome, sport=False):
    return (price, hours, outcome, sport)


def test_a_calibrated_market_shows_no_edge():
    # Priced 0.96, wins 96% of the time: carry earns exactly nothing.
    rows = [obs(0.96, 3, 1.0) for _ in range(96)] + [obs(0.96, 3, 0.0) for _ in range(4)]
    (b,) = calibrate(rows, min_price=0.90)
    assert b["n"] == 100
    assert round(b["edge"], 4) == 0.0


def test_underpriced_favourites_show_positive_edge():
    # Priced 0.96 but wins 99% — the only case worth trading.
    rows = [obs(0.96, 3, 1.0) for _ in range(99)] + [obs(0.96, 3, 0.0)]
    (b,) = calibrate(rows, min_price=0.90)
    assert b["edge"] > 0.02


def test_price_and_horizon_are_bucketed_separately():
    rows = [obs(0.99, 2, 1.0), obs(0.99, 100, 1.0), obs(0.92, 2, 1.0)]
    got = {(b["price_band"], b["hours"]) for b in calibrate(rows, min_price=0.90)}
    assert got == {("0.99-1.00", "< 6h"), ("0.99-1.00", "3-14d"), ("0.90-0.95", "< 6h")}


def test_bands_below_the_floor_are_not_reported():
    rows = [obs(0.55, 2, 1.0), obs(0.99, 2, 1.0)]
    assert [b["price_band"] for b in calibrate(rows, min_price=0.90)] == ["0.99-1.00"]


class FakeConn:
    """Minimal stand-in for the archive connection."""
    def __init__(self, rows): self.rows = rows
    def execute(self, sql, *a):
        return iter(self.rows) if "price_history" in sql else iter(())


def test_staleness_tolerates_an_irregular_bar_grid():
    """CLOB bars land 46-53s apart, not on the minute. Exact-timestamp
    lookup would silently find nothing and report no staleness at all."""
    from algorithms.resolution_carry.calibration import staleness

    # Bars drifting off the minute, price stepping 0.01 each bar.
    rows = [("t1", 1000 + i * 53, 0.96 + i * 0.01) for i in range(12)]
    out = staleness(FakeConn(rows), {"t1": (1.0, 0, False)},
                    min_price=0.95, horizons_min=(1,))
    assert out and out[0]["n"] > 0
    assert out[0]["median"] > 0
