# Copy Trade Algorithm — Design

A wallet-mirroring strategy: poll Polymarket's activity feed for a target
wallet's trades, classify each into a `BUY` / `SELL` / `MERGE` / `REDEEM`
signal, and emit an `Intent` for the shared runner to execute. We never
take a position the target doesn't have, and we size every position to a
*tier* derived from the target's total holding in that market.

The algorithm produces Intents only — it never places orders, writes the
DB, or talks to Telegram. All of that lives in `bot/runner.py`.

---

## Tier model — total position size, not per-signal bet

We don't bet "$1 every time the target buys". We hold a *total* position
whose size depends on how much the target is holding right now:

| Target's total holding ($) | Tier | Our total position cost ($) |
|---|---|---|
| `< tier1_min` (default $80k) | — | $0 (skip / fully exit) |
| `[tier1_min, tier1_max]` ($80k–$150k) | 1 | `tier1_size` ($1) |
| `(tier1_max, tier2_max]` ($150k–$300k) | 2 | `tier2_size` ($2) |
| `(tier2_max, ∞)` (> $300k) | 3 | `tier3_size` ($3) |

Defaults in `params.py`; everything tunable per algorithm instance.

**Why total cost, not per-signal:** if the target adds to a position
several times in a row, "$1 per buy" would balloon our exposure
non-linearly with their conviction. The tier model caps us at a fixed
total regardless of how many fills it took the target to build their
position.

---

## Signal types

Each activity row from the Polymarket Data API parses (via `bot/fetcher.py`)
into one of four actions. The algorithm emits at most one Intent per row.

### BUY — tier top-up

Look up the target's *current* total holding in the market (via
`fetch_target_position_value`, which retries past Data API eventual
consistency). Decide the tier from that holding. Compute the gap between
our current position cost and the tier target:

```
gap = tier_for_holding(target_holding)  −  our_current_cost
```

- `target_holding < tier1_min` → **skip** entirely.
- `gap ≤ 0` → already at/above the tier target → **skip** (no top-up).
- `gap < min_order_size_usdc` → **skip** (Polymarket rejects sub-$1 orders).
- Otherwise emit `OpenIntent(usdc_amount=gap, signal_price=target_fill_price)`.

The runner then runs risk checks, slippage gate, and places (or simulates)
the BUY.

### SELL — tier resize (NOT proportional)

The symmetric counterpart of BUY: figure out what tier the target lands
in *after* the sell, and resize our position to match.

```
post_sell_holding = cached_pre_sell_holding − target_sell_size
new_target_cost   = tier_for_holding(post_sell_holding)     # or 0 if below tier1_min
fraction_to_close = (our_current_cost − new_target_cost) / our_current_cost
```

Outcomes:

| Target before | Target sells | Target after | New tier → cost | Our action |
|---|---|---|---|---|
| $400k (tier 3, $3 ours) | $50k | $350k | tier 3, still $3 | **No-op** |
| $400k (tier 3, $3 ours) | $300k | $100k | tier 1, $1 | Close 2/3 → $1 remains |
| $400k (tier 3, $3 ours) | $350k | $50k | below tier1_min, $0 | **Full close** |
| $200k (tier 2, $2 ours) | $80k | $120k | tier 1, $1 | Close 1/2 → $1 remains |
| $150k (tier 1, $1 ours) | $80k | $70k | below tier1_min, $0 | **Full close** |

**Cache miss → full close.** Without a cached pre-sell holding we can't
compute the post-sell tier, so the safe default is to exit completely.
This happens after process restarts (for markets we never observed a BUY
in) and is logged.

**Cache update.** The cache is set to `post_sell_holding` (clamped at 0)
on every SELL, including no-op resizes, so the next sell on the same
market re-tiers off the correct value.

### MERGE — full close

A merge means the target redeemed complementary YES+NO shares for $1 per
pair, fully exiting their directional bet. We emit a `CloseIntent` with
`fraction=1.0` and `signal_price=0` (which disables the slippage gate in
the runner, since this is a forced exit). Cache is zeroed.

Without this handler, our position would sit open forever — the target
will never emit a regular SELL signal for it. See the rationale that
prompted adding MERGE handling in the commit history.

### REDEEM — settle at canonical close price

Target redeemed a resolved market. We emit `SettleIntent`. The runner
checks Gamma for an authoritative `closed`/`resolved` flag and settles
our position at the outcome price (capped to 0 or 1 if the CLOB price is
near the rails), booking realized P&L. If neither Gamma nor CLOB gives a
confident answer, we leave the position open and warn.

---

## State

All algorithm state is instance-level, so multiple `CopyTradeAlgorithm`
instances (different target wallets, different profiles) coexist safely.

| State | Purpose | Lifetime |
|---|---|---|
| `self._address` | Resolved target wallet address | Set once in `setup()` |
| `self._seen_ids` | Bounded LRU of activity tx hashes already dispatched | Process lifetime |
| `self.holding_cache` | `TargetHoldingCache` — last observed $ holding per market | Process lifetime |
| `self._tracker` | DB-backed position tracker scoped to this algorithm's `algo` namespace | Process lifetime |
| `self._paper` | Resolved at `setup()` from params.mode + CLOB client availability | Process lifetime |

`seen_ids` caps at `SEEN_IDS_MAX` (5000) entries to bound memory; oldest
entries get evicted LRU-style. On startup, the current activity tail is
loaded into `seen_ids` so we don't re-execute history.

`holding_cache` is the linchpin of accurate SELL resizing. BUY signals
populate it (via `fetch_target_position_value`); SELL signals consume and
update it; MERGE zeroes it. The cache is *not* persisted — a process
restart leaves it empty, which is why cache-miss SELL falls back to full
close.

---

## Polling lifecycle

`Algorithm.poll()` is called on a per-algorithm cadence
(`params.poll_interval_seconds`, default 20s) by the main.py worker
thread:

1. `fetcher.fetch_recent_trades(self._address)` — Data API call.
2. Filter out anything below `params.min_trade_size_usdc` (dust).
3. Skip rows already in `seen_ids`.
4. Sort the new rows by timestamp (the API doesn't guarantee order).
5. For each: add to `seen_ids`, classify, yield Intent(s).

The runner picks up each Intent and routes it through the shared
slippage / risk / fill / DB / notifier pipeline.

---

## Modes and isolation

`params.mode` (`Mode.PAPER` / `Mode.LIVE`) is read at startup and threaded
into the worker as a single `paper` flag. Paper mode simulates fills
against the current CLOB price (with `paper_fee_bps` modeled fees) and
maintains a per-algorithm `paper_account.balance` row in the DB. Live
mode uses the shared CLOB client to place real orders.

Two instances of `CopyTradeAlgorithm` in the same process — say, one in
`Mode.LIVE` for prod and another in `Mode.PAPER` for an experiment — write
to different `algo` namespaces in the same SQLite file. They never touch
each other's positions, trade log, or paper bankroll. For full process
separation, give each profile its own `DB_PATH` in `.env.<profile>` (see
the top-level README and the profile bundle in `algorithms/profiles/`).

---

## Edge cases and known limitations

**Target holds both YES and NO simultaneously.** `fetch_target_position_value`
sums all rows for a market. A SELL on one side decrements the cache by
the sell notional regardless of which side — fine for sizing decisions,
but conceptually it conflates the two sides' tier contributions. The
MERGE handler covers the most common case (the target paired up to exit
both sides at once).

**API eventual consistency.** `fetch_target_position_value` retries until
the API reflects at least `expected_min` (the BUY notional just observed).
Without this, the BUY top-up could see a stale "before" value and
under-size our position.

**Slippage gate on partial closes.** SELL signals carry the target's fill
price, so the slippage check is active. A target who sold at 0.50 with
the CLOB now at 0.40 will trigger a 20%-drift skip and our resize won't
happen this tick. The next SELL in the same market re-tiers from the
updated cache, so we converge over time as long as the slippage cap is
calibrated correctly.

**Per-order minimum.** Polymarket rejects orders below ~$1. The BUY path
guards with `params.min_order_size_usdc`. A SELL might leave us with a
tiny dust position if the resize amount also falls under the floor — in
practice this only happens with mis-tuned tier sizes (e.g. `tier1_size`
of $1.50 when `min_order_size_usdc` is also $1.50).

**Position keyed by `market_id`, not by outcome.** The DB stores one row
per `(market_id, paper, algo)`. If the bot somehow opened positions on
both YES and NO in the same market (shouldn't happen via this algorithm,
but could happen if a stray manual write created one), records would
collide. The algorithm only ever buys the side the target bought, so this
is safe in practice.

---

## Configuration

All knobs live in `params.py` as a frozen `@dataclass`. Env-var
overrides via the `COPYTRADE_*` prefix, with legacy unprefixed names
accepted as a fallback. The most commonly tuned ones:

| Knob | Default | What it controls |
|---|---|---|
| `target_address` / `target_username` | unset | The wallet we mirror |
| `mode` | `paper` | Paper vs live |
| `poll_interval_seconds` | 20 | How often we re-check activity |
| `tier{1,2,3}_size` | 1 / 2 / 3 | Total position cost at each tier |
| `tier1_min` / `tier1_max` / `tier2_max` | 80k / 150k / 300k | Tier thresholds (USD holding) |
| `max_position_size_usdc` | 3 | Per-market cap enforced by RiskManager |
| `max_total_exposure_usdc` | 12 | Total open exposure cap |
| `daily_loss_limit_usdc` | 4 | Auto-suspend new BUYs when down this much today |
| `min_order_size_usdc` | 1 | Polymarket's protocol minimum |
| `max_slippage` | 0.05 | Skip orders drifting >5% from signal price |
| `paper_starting_balance` | 10000 | Seed for the per-algo paper bankroll |

The risk caps are per-algorithm, not per-process. Two instances each get
their own pool, so you can run a $12-exposure prod alongside a
$100-exposure paper experiment without crossing wires.

---

## File map

| File | Role |
|---|---|
| `algorithm.py` | `CopyTradeAlgorithm` class — signal classification + tier math |
| `params.py` | `CopyTradeParams` dataclass + env loading |
| `__init__.py` | Re-exports the class and params for profile bundles |
| `design.md` | This document |

The algorithm has no implementation of order placement, slippage gating,
DB writes, or notifications — all of that is generic, reusable, and lives
in `bot/runner.py`. Adding a second algorithm in the same project means
writing a new directory parallel to this one with the same three files —
no infrastructure code required.
