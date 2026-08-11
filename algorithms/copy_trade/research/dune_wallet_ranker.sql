-- Export normalized resolved BUYs for the ranked copy-trade workflow.
--
-- Run in Dune, export CSV, then import with:
--   PROFILE=ranked_paper python scripts/import_wallet_history.py result.csv
--
-- The query uses Dune's curated Polymarket tables. Keep the block-time
-- predicate: market_trades is partitioned by block_month.
--
-- For an inexpensive first pass, add an address predicate to each branch of
-- buy_entries (for example, AND maker IN (...) / AND taker IN (...)).

WITH buy_entries AS (
    SELECT
        lower(concat('0x', to_hex(maker))) AS wallet,
        asset_id,
        price AS entry_price,
        block_time AS entry_time
    FROM polymarket_polygon.market_trades
    WHERE block_time >= now() - INTERVAL '24' MONTH
      AND is_taker_side
      AND maker_side = 'BUY'

    UNION ALL

    SELECT
        lower(concat('0x', to_hex(taker))) AS wallet,
        asset_id,
        price AS entry_price,
        block_time AS entry_time
    FROM polymarket_polygon.market_trades
    WHERE block_time >= now() - INTERVAL '24' MONTH
      AND is_taker_side
      AND taker_side = 'BUY'
),
resolved_buys AS (
    SELECT
        e.wallet,
        e.entry_price,
        d.settlement_value AS outcome,
        d.resolved_on_timestamp,
        -- Conservative initial copyability proxy. Replace with a score based
        -- on entry-vs-+5m/+1h/+6h price movement before any live promotion.
        CAST(1.0 AS DOUBLE) AS copyability_score
    FROM buy_entries e
    JOIN polymarket_polygon.market_details d
      ON e.asset_id = d.token_id
    WHERE d.settlement_value IN (0.0, 1.0)
      AND d.resolved_on_timestamp IS NOT NULL
      AND e.entry_time < d.resolved_on_timestamp
)
SELECT
    wallet,
    entry_price,
    outcome,
    to_unixtime(resolved_on_timestamp) AS resolved_at,
    copyability_score
FROM resolved_buys
WHERE entry_price > 0 AND entry_price < 1
ORDER BY resolved_at, wallet;
