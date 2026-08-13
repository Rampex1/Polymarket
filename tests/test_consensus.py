"""Snapshot-consensus grouping. Pure functions — no DB, no network."""

from algorithms.copy_trade.consensus import find_consensus, parse_positions


def row(asset, opposite, outcome, cost, avg=0.30, cur=0.30, redeemable=False):
    return {
        "asset": asset, "oppositeAsset": opposite, "conditionId": "0xmkt",
        "outcome": outcome, "title": "Will X happen?", "endDate": "2026-07-20",
        "size": cost / avg, "avgPrice": avg, "initialValue": cost,
        "curPrice": cur, "redeemable": redeemable,
    }


def snapshot(spec):
    """{wallet: [row, ...]} → flat holdings."""
    return [h for w, rows in spec.items() for h in parse_positions(w, rows)]


def test_resolved_and_empty_rows_are_dropped():
    held = parse_positions("0xa", [
        row("yes", "no", "Yes", 100),
        row("yes", "no", "Yes", 100, redeemable=True),
        row("yes", "no", "Yes", 0),
    ])
    assert len(held) == 1


def test_opposing_sides_do_not_count_as_agreement():
    # 3 on YES, 3 on NO is maximum disagreement, not six-way consensus.
    spec = {f"0x{i}": [row("yes", "no", "Yes", 100)] for i in range(3)}
    spec.update({f"0x{i}": [row("no", "yes", "No", 100)] for i in range(3, 6)})
    assert find_consensus(snapshot(spec), min_support=3, min_margin=2) == []


def test_margin_survives_light_opposition():
    spec = {f"0x{i}": [row("yes", "no", "Yes", 100)] for i in range(5)}
    spec["0x9"] = [row("no", "yes", "No", 100)]
    (c,) = find_consensus(snapshot(spec), min_support=3, min_margin=2)
    assert (c.support, c.opposition, c.margin) == (5, 1, 4)


def test_hedged_wallet_is_not_its_own_opposition():
    spec = {f"0x{i}": [row("yes", "no", "Yes", 100)] for i in range(3)}
    spec["0x0"].append(row("no", "yes", "No", 50))  # holds both sides
    (c,) = find_consensus(snapshot(spec), min_support=3, min_margin=3)
    assert c.opposition == 0


def test_conviction_filter_drops_dart_throws():
    # 0x2 puts 1% of its book on the market; the others 100%.
    spec = {f"0x{i}": [row("yes", "no", "Yes", 100)] for i in range(2)}
    spec["0x2"] = [row("yes", "no", "Yes", 10), row("other", "x", "Yes", 990)]
    assert len(find_consensus(snapshot(spec), min_support=3, min_margin=1)) == 1
    assert find_consensus(snapshot(spec), min_support=3, min_margin=1, min_conviction=0.05) == []


def test_entry_is_cost_weighted_and_drift_measures_the_run():
    spec = {
        "0xa": [row("yes", "no", "Yes", 9_000, avg=0.10, cur=0.50)],
        "0xb": [row("yes", "no", "Yes", 1_000, avg=0.50, cur=0.50)],
        "0xc": [row("yes", "no", "Yes", 1_000, avg=0.50, cur=0.50)],
    }
    (c,) = find_consensus(snapshot(spec), min_support=3, min_margin=1)
    assert round(c.avg_entry, 4) == 0.1727      # not the 0.367 an unweighted mean gives
    assert round(c.drift, 2) == 1.89            # price nearly tripled their basis


def test_decided_markets_are_dropped_at_both_ends():
    def cohort(cur):
        return snapshot({f"0x{i}": [row("yes", "no", "Yes", 100, avg=0.40, cur=cur)]
                         for i in range(4)})
    # Already won: a finished match sits at 1.00 with nothing left to pay out.
    assert find_consensus(cohort(1.0), min_support=3, min_margin=1) == []
    # Already lost: a cohort down 98% is holding a fossil, not an opinion.
    assert find_consensus(cohort(0.01), min_support=3, min_margin=1) == []
    assert len(find_consensus(cohort(0.45), min_support=3, min_margin=1)) == 1
