"""
Position reconciliation — bot DB vs actual on-chain state.

Why this exists
---------------
The bot's local SQLite tracks only positions it placed itself, recorded
*after* the order succeeded. Two things can break that invariant:

  1. **Ghost positions**: an order executes on Polymarket but the bot
     dies (OOM, signal, network error) before `record_buy` commits.
     Polymarket has the position; the bot doesn't know.
  2. **Stale DB rows**: a position is closed off-bot (manual sale,
     market resolution that the bot missed, merge done by user) but
     the bot still thinks it holds shares.

Either case leads to wrong sizing on the next signal — the BUY path
sees `current_cost=0` and tops up the full tier amount, double-buying
the ghost position; the SELL path mirror-closes a position that no
longer exists, sending an order that gets rejected.

What we do
----------
At startup and on a periodic timer, fetch our actual Polymarket positions
from the Data API and diff against the DB. We **only log warnings** — we
do not auto-reconcile, because each kind of discrepancy needs a different
manual fix (cancel an in-flight order, manually book a sale, investigate
a stuck position).

Paper mode is a no-op (paper positions live in the bot DB by definition).
"""

import logging

from . import fetcher
from .ledger import Ledger

logger = logging.getLogger(__name__)

# Tolerance for share-count comparisons. CLOB / Data API may round; tiny
# rounding diffs shouldn't trigger warnings.
SHARES_EPSILON = 0.01
# Tolerance for USD value comparisons.
USD_EPSILON = 0.10


def reconcile_positions(
    tracker: Ledger,
    funder_address: str,
    algo_name: str,
    paper: bool,
) -> dict:
    """Compare bot DB to on-chain. Returns a summary dict for telemetry.

    The returned dict has keys: `ghost` (list of market_ids the wallet
    holds but bot doesn't), `stale` (market_ids the bot has but wallet
    doesn't), `divergent` (both sides have it but shares disagree),
    `agreed` (both match), `skipped` (paper or fetch failure).

    Logging is the primary side effect — telemetry callers can use the
    return value if they need to alert externally.
    """
    summary = {"ghost": [], "stale": [], "divergent": [], "agreed": [], "skipped": False}

    if paper:
        summary["skipped"] = True
        return summary

    if not funder_address:
        logger.debug("[%s] Reconcile skipped — no funder address.", algo_name)
        summary["skipped"] = True
        return summary

    onchain = fetcher.fetch_user_positions(funder_address)
    if not onchain:
        # Either there genuinely are no positions, or the API call failed.
        # Both are safe to log at debug level — if DB shows positions we
        # still expect, the loop below will warn about them.
        logger.debug(
            "[%s] On-chain returned no positions for %s.",
            algo_name, funder_address[:10],
        )

    # Index on-chain positions by market_id. The Data API uses `conditionId`
    # for the market; rows also carry `asset` (token id), `size` (shares),
    # and `value` (current USD value).
    onchain_by_market: dict[str, dict] = {}
    for row in onchain:
        market_id = row.get("conditionId") or row.get("market_id")
        if not market_id:
            continue
        # If a wallet holds both sides of the same market, sum the rows so
        # the comparison is at the market level (which is how our DB keys).
        existing = onchain_by_market.get(market_id)
        size = float(row.get("size") or 0)
        value = float(row.get("value") or row.get("currentValue") or 0)
        if existing is None:
            onchain_by_market[market_id] = {
                "size": size, "value": value, "asset_id": row.get("asset", ""),
            }
        else:
            existing["size"] += size
            existing["value"] += value

    db_positions = {p.market_id: p for p in tracker.all_open(paper=False)}

    # 1. Stale: bot DB has, on-chain doesn't.
    for market_id, p in db_positions.items():
        if market_id not in onchain_by_market:
            logger.warning(
                "[%s] STALE: DB has %.2f shares ($%.2f cost) in %s, "
                "on-chain has none. Position may have been closed off-bot.",
                algo_name, p.shares, p.total_cost_usdc, market_id[:14],
            )
            summary["stale"].append(market_id)

    # 2. Ghost: on-chain has, bot DB doesn't.
    for market_id, oc in onchain_by_market.items():
        if market_id not in db_positions:
            if oc["value"] < USD_EPSILON:
                continue   # dust, ignore
            logger.warning(
                "[%s] GHOST: on-chain has %.2f shares ($%.2f value) in %s, "
                "DB has none. Bot will not manage this position — possibly "
                "pre-existing, or a fill the bot didn't record.",
                algo_name, oc["size"], oc["value"], market_id[:14],
            )
            summary["ghost"].append(market_id)

    # 3. Divergent: both have it but shares disagree.
    for market_id, p in db_positions.items():
        oc = onchain_by_market.get(market_id)
        if oc is None:
            continue
        if abs(p.shares - oc["size"]) > SHARES_EPSILON:
            logger.warning(
                "[%s] DIVERGENT: %s — DB has %.2f shares, on-chain has %.2f. "
                "Partial off-bot activity?",
                algo_name, market_id[:14], p.shares, oc["size"],
            )
            summary["divergent"].append(market_id)
        else:
            summary["agreed"].append(market_id)

    n = (len(summary["agreed"]) + len(summary["stale"])
         + len(summary["ghost"]) + len(summary["divergent"]))
    if n == 0:
        logger.info("[%s] Reconcile: no positions on either side.", algo_name)
    else:
        logger.info(
            "[%s] Reconcile: %d agreed, %d stale, %d ghost, %d divergent.",
            algo_name, len(summary["agreed"]), len(summary["stale"]),
            len(summary["ghost"]), len(summary["divergent"]),
        )

    # Discrepancies are surfaced via logs only — Discord alerts proved
    # noisy in practice (manual UI trades show up as ghosts forever; UI
    # closes show up as stale until cleared). Re-enable here if a specific
    # category becomes worth paging on.

    return summary
