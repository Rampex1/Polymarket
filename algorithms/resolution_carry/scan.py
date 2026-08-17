"""
One Gamma scan, shared by every arm of the A/B.

Each arm is a worker thread polling every 15 seconds, and each was paging the
same 2,100-market universe for itself — three identical scans, 63 HTTP
requests per cycle, on a 498MB box that also runs the archiver and the
Discord bot. The arms trade the *same* universe by construction: they differ
in which side they take and how they enter, never in what exists to be
bought. So the scan is done once per interval and shared.

What is shared is deliberately not the finished candidate list. The arms do
not screen alike — the underdog arm is pinned to the original 0.955-0.985
band while the control moved to 0.980-0.990 — so each still runs its own
`screen.evaluate` and gets its own funnel. What is shared is the expensive
part: the HTTP.

Two things are kept from a scan, and the split is what keeps this cheap:

  * `rows` — full market dicts, but only for the *widest band any arm
    trades*. That gate alone rejects ~99% of the universe, so the cache holds
    tens of rows rather than 2,100. Gamma rows are fat (nested events, tags,
    outcomes); holding them all was 34.5MB a poll, which is what the
    page-at-a-time streaming was introduced to avoid. Streaming is preserved
    here — pages are filtered as they arrive and only survivors retained.

  * `asks` — `market_id -> bestAsk` for *everything* scanned, which is a
    string and a float per market. Held positions drift out of the band as
    they resolve, so low-water tracking cannot read them off `rows`; it only
    ever needed the price.

The band union is computed from `params.py` rather than declared, so adding
an arm with a wider band widens the prefilter automatically instead of
silently starving it.
"""

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import screen
from .params import VARIANTS, ResolutionCarryParams

logger = logging.getLogger(__name__)

# Gamma caps a /markets page at 100 rows regardless of `limit`, and 422s on
# any offset at or past 2100 — verified against both closed=true and
# closed=false, so it is a platform ceiling, not the end of our window.
GAMMA_PAGE = 100
GAMMA_MAX_OFFSET = 2100


@dataclass
class ScanResult:
    """One universe sweep, in the two shapes the arms actually consume."""

    rows: list[dict] = field(default_factory=list)
    asks: dict[str, float] = field(default_factory=dict)
    funnel: Counter = field(default_factory=Counter)
    scanned: int = 0
    fetched_at: float = 0.0


def arm_params() -> list[ResolutionCarryParams]:
    """Every arm this strategy can run, control first."""
    return [ResolutionCarryParams()] + [
        ResolutionCarryParams(name=name) for name in VARIANTS
    ]


def union_band() -> tuple[float, float]:
    """The loosest price band across all arms — the prefilter's bounds.

    Derived rather than declared: a new arm with a wider band widens this on
    its own. Getting it wrong in the narrow direction would silently starve
    an arm, which is the one failure this must not have.
    """
    arms = arm_params()
    return min(a.min_ask for a in arms), max(a.max_ask for a in arms)


def union_window() -> tuple[float, float, int]:
    """(hours past end, days ahead, pages) wide enough for every arm."""
    arms = arm_params()
    return (
        max(a.max_hours_past_end for a in arms),
        max(a.max_days_to_resolution for a in arms),
        max(a.discovery_pages for a in arms),
    )


class SharedScan:
    """Caches one sweep for `ttl` seconds and hands it to every caller.

    The lock is held across the fetch on purpose. A second arm arriving
    mid-scan waits ~6s and then gets that scan, which is both fresher and
    cheaper than starting its own.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._result = ScanResult()

    def reset(self) -> None:
        """Drop the cached sweep. For tests — a process-wide cache that
        survives between them makes one test's universe another's."""
        with self._lock:
            self._result = ScanResult()

    def get(self, market_data, ttl: float, now: Optional[float] = None) -> ScanResult:
        now = time.time() if now is None else now
        with self._lock:
            if self._result.fetched_at and now - self._result.fetched_at < ttl:
                return self._result
            self._result = self._sweep(market_data, now)
            return self._result

    def _sweep(self, market_data, now: float) -> ScanResult:
        lo, hi = union_band()
        past_hours, ahead_days, pages = union_window()
        stamp = datetime.now(timezone.utc)
        # Reach back by the grace window, or Gamma filters post-whistle
        # markets out before any arm's screen can see them.
        end_min = (stamp - timedelta(hours=past_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_max = (stamp + timedelta(days=ahead_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        result = ScanResult(fetched_at=now)
        for page_no in range(max(1, pages)):
            offset = page_no * GAMMA_PAGE
            if offset >= GAMMA_MAX_OFFSET:
                break
            batch = market_data.top_markets(
                closed=False, limit=GAMMA_PAGE, offset=offset,
                end_date_min=end_min, end_date_max=end_max,
            )
            result.scanned += len(batch)
            for row in batch:
                market_id = str(row.get("conditionId") or "")
                ask = screen.to_float(row.get("bestAsk"))
                if market_id and ask is not None:
                    result.asks[market_id] = ask
                if ask is not None and lo <= ask <= hi:
                    result.rows.append(row)
                else:
                    # Counted here so each arm's funnel still adds up to the
                    # full universe even though it never sees these rows.
                    result.funnel["out of band"] += 1
            # A short page is the end of the list; so is the empty list Gamma
            # returns past its offset ceiling.
            if len(batch) < GAMMA_PAGE:
                break
        logger.debug(
            "shared scan: %d markets, %d in the %.3f-%.3f union band",
            result.scanned, len(result.rows), lo, hi,
        )
        return result


# One per process. The arms are threads inside it, so this is what they share.
SHARED = SharedScan()
