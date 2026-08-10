"""
caches.py

Small in-memory helpers for polling loops. No I/O, no persistence — both
are per-instance so two workers watching different wallets never share
state, and both are bounded so a long-running process can't leak.
"""

import collections
import threading
from typing import Optional


SEEN_IDS_MAX = 5000


class SeenRing:
    """Bounded LRU set of already-processed signal ids.

    The shared dedupe primitive for every polling loop (copy_trade,
    insider_flow, the utility `poll` below). Re-marking an id refreshes its
    recency; past `maxlen` the least-recently-marked id falls off.
    """

    def __init__(self, maxlen: int = SEEN_IDS_MAX) -> None:
        self._maxlen = maxlen
        self._ids: collections.OrderedDict[str, None] = collections.OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def mark(self, key: str) -> None:
        if key in self._ids:
            self._ids.move_to_end(key)
        else:
            self._ids[key] = None
            if len(self._ids) > self._maxlen:
                self._ids.popitem(last=False)


class TargetHoldingCache:
    """Thread-safe in-memory cache of the target's last-observed holding.

    One instance per algorithm — algorithms that copy a wallet hold this as
    instance state so multiple copy-trade algorithms (different targets) can
    run in parallel without sharing cache.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, float] = {}

    def set(self, market_id: str, value: float) -> None:
        with self._lock:
            self._data[market_id] = max(0.0, float(value))

    def get(self, market_id: str) -> Optional[float]:
        with self._lock:
            return self._data.get(market_id)

    def decrement(self, market_id: str, amount: float) -> None:
        with self._lock:
            if market_id in self._data:
                self._data[market_id] = max(0.0, self._data[market_id] - float(amount))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
