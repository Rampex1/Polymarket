# CLAUDE.md — Polymarket Copy-Trading Bot

## Project Overview

A Python bot that automatically mirrors trades from one or more target Polymarket wallets. It detects BUY/SELL/MERGE/REDEEM signals via API polling, applies tiered bet-sizing, enforces risk limits, and runs each strategy in paper (simulated) or live mode.

The core thesis baked into the default strategy: only copy *high-conviction* positions from a target — sizing keys off the **target's absolute holding** in a market (default floor $80k), not their per-trade size.

## Running the Bot

```bash
source .venv/bin/activate
python main.py                          # default profile
PROFILE=prod python main.py             # live profile → loads .env.prod
PROFILE=experimental python main.py     # paper A/B profile → loads .env.experimental
```

There is **no global paper/live flag**. Mode is per-algorithm (`Mode.PAPER` / `Mode.LIVE`), declared in each algorithm's params. A single process can run prod-live and paper-experimental algorithms side by side.

**Utility scripts:**
```bash
python scripts/reset_paper_trade_db.py      # Wipe and reset paper trading DB
python scripts/trading_account_summary.py   # Print portfolio snapshot
bash   scripts/ssh_vm.sh                     # SSH into the deployment VPS
```

## Architecture

The bot is split into **shared infrastructure** (`bot/`) and **pluggable strategies** (`algorithms/`). Each strategy is an `Algorithm` that yields `Intent`s; a shared, stateless `runner` turns intents into orders. Writing a new strategy means subclassing `Algorithm` and inheriting all execution/risk/notify logic for free.

```
main.py                   # Entry point — one worker thread per enabled algorithm
bot/
  config.py               # Bot-wide infra only: API URLs, creds, DB path, Discord, timezone
  algorithm.py            # Algorithm ABC + Intent types (Open/Close/Settle) + Mode + AlgoParams protocol
  runner.py               # Shared dispatch: slippage gate, CLOB orders, paper fills, DB writes, notify
  models.py               # Trade dataclass (legacy interface the runner adapts intents into)
  db.py                   # SQLite, thread-local connections, WAL, per-algo schema + migrations
  fetcher.py              # Poll Data API for a wallet's trades; wallet lookup; resolution-price helpers
  positions.py            # PositionTracker (DB CRUD) + RiskManager (enforce limits)
  reconciliation.py       # Diff bot DB vs on-chain positions (live only); logs + Discord alerts
  notifier.py             # Discord alerts + midnight daily summary thread
algorithms/
  __init__.py             # Registry — PROFILE env var selects which profile's ALGORITHMS run
  copy_trade/             # The one shipped strategy
    algorithm.py          # CopyTradeAlgorithm: poll target → tier sizing → emit intents
    params.py             # CopyTradeParams dataclass (COPYTRADE_* env-driven, legacy fallbacks)
  profiles/
    default.py            # env-driven single CopyTradeAlgorithm (no PROFILE set)
    prod.py               # Mode.LIVE algorithms — real money, keep conservative
    experimental.py       # Mode.PAPER variants for tuning / A/B testing
scripts/
  reset_paper_trade_db.py
  trading_account_summary.py
  ssh_vm.sh
tests/                    # pytest suite (copy_trade, db, fetcher, positions, risk, runner, ...)
```

### Per-algorithm worker model

Each algorithm in the selected profile runs as a fully independent worker thread (`main.py:_run_worker`):
  * Own `PositionTracker(algo=...)` — DB rows partitioned by the `algo` column.
  * Own `RiskManager(tracker, params)` — caps come from the algorithm's params.
  * Own poll cadence (`params.poll_interval_seconds`).
  * Own paper bankroll (per-algo row in `paper_account`).
  * Own daily-summary thread tagged with the algo name.
  * A crash in one algorithm does not affect the others.

The shared CLOB client is built **once**, and only if at least one enabled algorithm is `Mode.LIVE`. If an algorithm declares `LIVE` but no client/creds exist, the worker falls back to paper to avoid silently mis-routing real-money orders.

### Trade lifecycle (copy_trade)

1. `CopyTradeAlgorithm.poll()` fetches the target's recent activity (Data API), dedupes by tx hash, and classifies each new row.
2. It translates each into an `Intent` and yields it:
   * **BUY** → `OpenIntent`. Looks up the target's *total* holding in the market, maps it to a tier, and tops our position up so its total cost equals the tier target. Skips if below tier-1 floor or if the top-up is under the min order.
   * **SELL** → `CloseIntent`. Resizes our position down to the target's post-sell tier (uses a cached pre-sell holding to compute the fraction; cache miss → full close).
   * **MERGE** → `CloseIntent(fraction=1.0)`. Target exited via complementary YES+NO redemption → full close, no slippage gate.
   * **REDEEM** → `SettleIntent`. Market resolved → settle at the canonical close price.
3. `runner.dispatch()` executes the intent: risk check → slippage gate → place order (or simulate in paper) → record position → Discord notify.
4. Periodically (live only) `reconciliation.reconcile_positions()` diffs the DB against on-chain holdings and warns on ghost/stale/divergent positions.

## Key Configuration

### Bot-wide infrastructure (`.env` / `.env.<profile>`, read by `bot/config.py`)

| Variable | Notes |
|---|---|
| `PROFILE` | Selects `algorithms/profiles/<name>.py` and prefers `.env.<profile>`. Default `default`. |
| `DB_PATH` | SQLite file path (default `positions.db`) |
| `DISCORD_WEBHOOK_URL` | Discord incoming webhook (optional) |
| `TIMEZONE` | Daily-summary rollover tz (default `UTC`) |
| `POLY_PRIVATE_KEY` / `POLY_FUNDER_ADDRESS` / `POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE` | Live trading only. `POLY_FUNDER_ADDRESS` is your Polymarket **proxy wallet** (from the profile URL) and is required for live — without it orders sign correctly but debit the wrong account. |

### Per-algorithm settings (`algorithms/copy_trade/params.py`, `COPYTRADE_*` env vars)

Canonical names are `COPYTRADE_*`; legacy unprefixed names (`TARGET_ADDRESS`, `TIER1_SIZE`, …) are still accepted as a fallback.

| Variable (canonical) | Legacy | Default | Notes |
|---|---|---|---|
| `COPYTRADE_MODE` | `PAPER_TRADE` | `paper` | `paper` or `live` (per-algorithm) |
| `COPYTRADE_TARGET_ADDRESS` | `TARGET_ADDRESS` | — | Target proxy wallet; or set username |
| `COPYTRADE_TARGET_USERNAME` | `TARGET_USERNAME` | — | Resolved to a wallet via Gamma `/profiles` |
| `COPYTRADE_POLL_INTERVAL` | `POLL_INTERVAL_SECONDS` | `20` | Poll cadence (seconds) |
| `COPYTRADE_MIN_TRADE_SIZE` | `MIN_TRADE_SIZE_USDC` | `0.0` | Ignore target trades smaller than this |
| `COPYTRADE_TIER1_MIN` | `TIER1_MIN` | `80000` | Holding below this → skip entirely |
| `COPYTRADE_TIER1_MAX` / `TIER1_SIZE` | `TIER1_MAX` / `TIER1_SIZE` | `150000` / `1.0` | Tier 1: $1 bet |
| `COPYTRADE_TIER2_MAX` / `TIER2_SIZE` | `TIER2_MAX` / `TIER2_SIZE` | `300000` / `2.0` | Tier 2: $2 bet |
| `COPYTRADE_TIER3_SIZE` | `TIER3_SIZE` | `3.0` | Tier 3: $3 bet (>$300k holding) |
| `COPYTRADE_MAX_POSITION` | `MAX_POSITION_SIZE_USDC` | `3.0` | Max spend per market |
| `COPYTRADE_MAX_EXPOSURE` | `MAX_TOTAL_EXPOSURE_USDC` | `12.0` | Max total open exposure (this algo's pool) |
| `COPYTRADE_DAILY_LOSS_LIMIT` | `DAILY_LOSS_LIMIT_USDC` | `4.0` | Suspend buys if down this much today |
| `COPYTRADE_MIN_ORDER` | `MIN_ORDER_SIZE_USDC` | `1.0` | Skip top-ups smaller than this |
| `COPYTRADE_MAX_SLIPPAGE` | `MAX_SLIPPAGE` | `0.05` | Skip order if price moved > this fraction |
| `COPYTRADE_ORDER_TYPE` | `ORDER_TYPE` | `market` | `market` (FOK) or `limit` (GTC) |
| `COPYTRADE_PAPER_BALANCE` | `PAPER_STARTING_BALANCE` | `10000.0` | Virtual balance (paper) |
| `COPYTRADE_PAPER_FEE_BPS` | `PAPER_FEE_BPS` | `0` | Modeled paper fee (basis points) |

Tier knobs are per-instance, so profiles can run multiple copy-trade variants with different sizing (see `experimental.py`).

## External APIs

| API | Base URL | Used For |
|---|---|---|
| Gamma | `https://gamma-api.polymarket.com` | Username → proxy wallet (`/profiles`); market resolution (`/markets`) |
| Data | `https://data-api.polymarket.com` | Trade history (`/activity`), current positions (`/positions`) |
| CLOB | `https://clob.polymarket.com` | Order placement, last-trade price |

## Database Schema (SQLite)

Thread-local connections, WAL mode. All rows are partitioned by an `algo` column so multiple algorithms share one DB without collisions.

- `positions` — open positions, PK `(market_id, paper, algo)`
- `trade_log` — all executed trades (BUY/SELL/REDEEM), tagged with `algo`
- `daily_stats` — per-(date, algo) realized P&L
- `paper_account` — virtual cash balance, PK `algo`

`db._migrate()` upgrades older v0/v1 databases in place (adds `paper`/`algo` columns, repartitions PKs) idempotently.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values (or create .env.<profile> per profile)
```

## Testing

```bash
source .venv/bin/activate
pytest                  # full suite
pytest tests/test_copy_trade.py
```

## Deployment

Runs on a VPS (plain Python process under a virtualenv; Docker has been removed). `scripts/ssh_vm.sh` is a helper for SSH access. Persist `positions.db` and the appropriate `.env.<profile>` on the host.
