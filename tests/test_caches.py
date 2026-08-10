"""
In-memory helpers used by every polling loop.

SeenRing is what stops a strategy re-executing a trade it already acted on.
Its bound matters as much as its dedupe: an unbounded set leaks in a process
that runs for weeks, and an over-eager one re-executes an old signal.
"""

from bot.caches import SeenRing, TargetHoldingCache


# ── SeenRing ────────────────────────────────────────────────────────────────


def test_marks_are_deduped():
    ring = SeenRing(maxlen=10)
    ring.mark("a")
    assert "a" in ring
    ring.mark("a")
    assert len(ring) == 1


def test_evicts_the_least_recently_marked():
    """Past maxlen the oldest id falls off, so it would be re-executed."""
    ring = SeenRing(maxlen=3)
    for key in ("id0", "id1", "id2"):
        ring.mark(key)

    ring.mark("id3")                 # overflows — id0 is the oldest

    assert "id0" not in ring
    assert {"id1", "id2", "id3"} <= set(k for k in ("id1", "id2", "id3") if k in ring)
    assert len(ring) == 3


def test_remarking_refreshes_recency():
    """A re-marked id must not be the next one evicted."""
    ring = SeenRing(maxlen=3)
    for key in ("id0", "id1", "id2"):
        ring.mark(key)

    ring.mark("id0")                 # id1 is now the oldest
    ring.mark("id3")

    assert "id0" in ring
    assert "id1" not in ring


# ── TargetHoldingCache ──────────────────────────────────────────────────────


def test_holding_cache_round_trips_and_decrements():
    cache = TargetHoldingCache()
    assert cache.get("m1") is None

    cache.set("m1", 100.0)
    assert cache.get("m1") == 100.0

    cache.decrement("m1", 30.0)
    assert cache.get("m1") == 70.0


def test_holding_cache_never_goes_negative():
    """An oversized sell must floor at zero, not invert the close fraction."""
    cache = TargetHoldingCache()
    cache.set("m1", 10.0)
    cache.decrement("m1", 999.0)
    assert cache.get("m1") == 0.0


def test_holding_cache_instances_are_independent():
    """Two copy-trade workers on different wallets must not share state."""
    a, b = TargetHoldingCache(), TargetHoldingCache()
    a.set("m1", 100.0)
    assert b.get("m1") is None
