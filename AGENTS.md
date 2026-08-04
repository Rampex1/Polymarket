# AGENTS.md — Polymarket Copy-Trading Bot

## Codex Behavior

- After implementing changes, **commit automatically** (no need to ask).
- **Do not push** until the user explicitly says to.

## Project Overview

A Python bot that automatically mirrors trades from one or more target Polymarket wallets. It detects BUY/SELL/MERGE/REDEEM signals via API polling, applies tiered bet-sizing, enforces risk limits, and runs each strategy in paper (simulated) or live mode.

The core thesis baked into the default strategy: only copy *high-conviction* positions from a target — sizing keys off the **target's absolute holding** in a market (default floor $80k), not their per-trade size.

## Running the Bot

```bash
source .venv/bin/activate
PROFILE=prod python main.py             # live profile → loads .env.prod
PROFILE=experimental python main.py     # paper A/B profile → loads .env.experimental
```

`PROFILE` is **required** — there is deliberately no default profile, so the bot can never run under an implicitly-selected config. A bare `python main.py` exits with an error naming the available profiles.

There is **no global paper/live flag**. Mode is per-algorithm (`"paper"` / `"live"`), declared per algorithm block in `config/<profile>.toml`. A single process can run prod-live and paper-experimental algorithms side by side.

**Configuration lives in three places, by kind:**
- `config/<profile>.toml` — *behavior*: which algorithms run, their mode, targets, tiers, risk caps. A profile must set top-level `allow_live = true` before any `mode = "live"` block is accepted. Committed to git; tuning and prod promotion are TOML edits, never code edits.
- `config/webhooks.toml` — *Discord webhook registry*: routes each PROFILE to its channel via per-block `profiles` lists. Committed (repo is private — rotate webhooks before ever going public).
- `.env` / `.env.<profile>` — *secrets only*: the five `POLY_*` creds. Never committed. Optional overrides (`DISCORD_WEBHOOK_URL`, `TIMEZONE`, `DB_PATH`, `POLY_SIGNATURE_TYPE`) exist but defaults/registry normally cover them.

**Config tooling:**
```bash
python -m bot.params                  # list algorithm types
python -m bot.params copy_trade       # every knob: default + doc
python -m bot.params --effective      # fully-resolved config for $PROFILE (* = non-default)
python -m bot.report                  # per-algo performance: P&L, signals, win rate vs odds
```

**Utility scripts:**
```bash
python scripts/reset_paper_trade_db.py      # Wipe and reset paper trading DB
python scripts/trading_account_summary.py   # Print portfolio snapshot
bash   scripts/ssh_vm.sh                     # SSH into the deployment VPS
bash   scripts/setup_vm.sh                   # On the VPS: zero-to-running deploy (pull, deps, env checks, restart all tmux sessions)
```

## Architecture

The bot is split into **shared infrastructure** (`bot/`), **pluggable strategies** (`algorithms/`), and **declarative deployment config** (`config/`). Each strategy is an `Algorithm` that yields `Intent`s; a shared, stateless `runner` turns intents into orders. Writing a new strategy means subclassing `Algorithm`, registering it in `algorithms/__init__.py:REGISTRY`, and referencing it by type in a profile TOML.

```
main.py                   # Entry point — one worker thread per enabled algorithm
config/
  prod.toml               # Profile: live algorithms — real money, keep conservative
  experimental.toml       # Profile: paper variants for tuning / A/B testing
  webhooks.toml           # Discord webhook registry — PROFILE → channel routing
bot/
  config.py               # Bot-wide infra only: API URLs, creds, DB path, Discord, timezone
  profile_loader.py       # config/<profile>.toml → [Algorithm]; fail-fast validation
  params.py               # CLI: knob discovery (`python -m bot.params [type] [--effective]`)
  report.py               # CLI: per-algo performance report (`python -m bot.report`)
  runs.py                 # Run provenance — stamps resolved params + git sha per boot
  algorithm.py            # Algorithm ABC + Intent types (Open/Close/Settle) + Mode + AlgoParams protocol
  runner.py               # Shared dispatch: slippage gate, CLOB orders, paper fills, DB writes, notify
  models.py               # Trade dataclass (legacy interface the runner adapts intents into)
  db.py                   # SQLite, thread-local connections, WAL, per-algo schema + migrations
  fetcher.py              # Poll Data API for a wallet's trades; wallet lookup; resolution-price helpers
  positions.py            # PositionTracker (DB CRUD) + RiskManager (enforce limits)
  reconciliation.py       # Diff bot DB vs on-chain positions (live only); logs + Discord alerts
  notifier.py             # Discord alerts + midnight daily summary thread
  signals.py              # Signal feature logging — training-data rows, outcome-labeled at settle
  sizing.py               # Kelly math (pure): fraction, implied belief, fractional-Kelly stake
algorithms/
  __init__.py             # REGISTRY (type → classes) + lazy ENABLED via profile_loader
  copy_trade/             # Mirror a known target wallet
    algorithm.py          # CopyTradeAlgorithm: poll target → tier sizing → emit intents
    params.py             # CopyTradeParams — pure schema: knobs, defaults, docs, validate()
  insider_flow/           # Copy suspicious fresh-wallet whale buys (no known target)
    algorithm.py          # InsiderFlowAlgorithm: /trades firehose → freshness filter → intents
    params.py             # InsiderFlowParams — pure schema: knobs, defaults, docs, validate()
discovery/
  archive.py              # Price-history archiver (CLOB drops history at resolution — hoard it)
data/                     # All SQLite files (positions.db, discovery_archive.db) — gitignored
research/                 # Strategy research notes + plans (discovery_plan.md, implementation_plan.md)
scripts/
  reset_paper_trade_db.py
  trading_account_summary.py
  ssh_vm.sh
tests/                    # pytest suite (copy_trade, insider_flow, archive, db, fetcher, ...)
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
| `PROFILE` | Selects `config/<name>.toml` and prefers `.env.<profile>`. **Required** — no default; the bot refuses to boot without it. |
| `DB_PATH` | SQLite file path (default `data/positions.db`; an existing legacy `./positions.db` keeps working with a warning) |
| `DISCORD_WEBHOOK_URL` | Optional override — webhooks normally resolve from `config/webhooks.toml` by PROFILE |
| `TIMEZONE` | Daily-summary rollover tz (default `America/Los_Angeles`) |
| `POLY_PRIVATE_KEY` / `POLY_FUNDER_ADDRESS` / `POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE` | Live trading only. `POLY_FUNDER_ADDRESS` is your Polymarket **proxy wallet** (from the profile URL) and is required for live — without it orders sign correctly but debit the wrong account. |

### Per-algorithm settings (`config/<profile>.toml`)

All algorithm behavior is declared in the profile TOML — env vars are **not** read for algorithm params. Each `[[algorithm]]` block sets `type` (registry key), `name` (DB partition key — keep stable once set; renaming orphans its bankroll/history), `mode` (`"paper"`/`"live"`, always explicit), and an optional `[algorithm.params]` table; omitted knobs use schema defaults.

The knob schemas (names, defaults, docs, boot-time `validate()`) live in `algorithms/<type>/params.py`. Don't enumerate them here — discover them with:

```bash
python -m bot.params copy_trade       # or insider_flow
python -m bot.params --effective      # what $PROFILE actually resolves to
```

The loader fails fast at boot on unknown keys, duplicate names, bad modes, and `validate()` violations — a typo in a TOML key is a crash, never a silent no-op.

**insider_flow** detects the documented insider fingerprint: **fresh wallets making large first bets at long odds**. Polls the platform-wide Data-API `/trades` firehose (cash-filtered server-side), screens markets against Gamma category/tags (sports = gambling, not signal) and a time-value gate (must resolve soon and out-earn an index fund for the wait — unknown end date fails closed), then vets each candidate wallet's age/history via one `/activity` page (unverifiable wallets are *not* copied — fail closed). Survivors are buffered for a window, ranked by conviction score, and only the top N are copied. Exits at market resolution via a periodic Gamma sweep. Its `max_slippage` default (0.10) is deliberately wider than copy_trade's — these signals move fast. Defaults are sized for a **~$20 prod bankroll** — scale via the profile TOML when capital grows.

### Discovery price archiver

```bash
python -m discovery.archive --once             # one pass (cron-friendly)
python -m discovery.archive --loop --every 3600
```

Snapshots CLOB `/prices-history` for active top-volume, recently-closed, and whale-touched markets into `discovery_archive.db` (own SQLite file, gitignored). **The public API drops price history once markets resolve**, so this should run continuously — it's the raw material for copy-execution backtests and insider lead-lag analysis. Research context lives in `research/discovery_plan.md` and `research/implementation_plan.md`.

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
- `signals` — one row per dispatched `OpenIntent` (executed or skipped): raw `features` JSON captured at signal time, `outcome`/`pnl_usdc` backfilled at settlement. Training data for confidence models — log raw observables, never derived scores.
- `runs` — one row per worker boot: resolved params JSON, profile, git sha. Lets `bot.report` attribute results to the exact config version that produced them.

`db._migrate()` upgrades older v0/v1 databases in place (adds `paper`/`algo` columns, repartitions PKs) idempotently.

## Discord Bot (slash commands)

Two-way interface — runs as a daemon thread alongside workers. Each profile (prod / experimental) runs its own bot instance; Discord shows them separately in the slash-command picker by application name.

**Commands:**
| Command | Description |
|---|---|
| `/status` | All algorithms: mode, exposure, today's P&L |
| `/positions [algo]` | Open positions, optionally filtered by algo name |
| `/pnl` | Realized P&L + exposure per algo and combined |
| `/summary` | Send the daily summary to the summary channel immediately |

Runs in its own tmux session (`discord`) independent of the trading workers — one bot, one token, all profiles. If a worker crashes the bot stays up; if the bot crashes the workers keep trading.

**One-time setup:**
1. [discord.com/developers](https://discord.com/developers) → **New Application** (one bot total, e.g. "Polymarket Bot") → **Bot** → Reset Token → copy it
2. **OAuth2** → URL Generator → scopes: `bot` + `applications.commands` → bot permissions: `Send Messages`, `Use Slash Commands` → open invite URL → add to server
3. Add to the shared `.env` on the VPS:
   ```
   DISCORD_BOT_TOKEN=<token>
   DISCORD_GUILD_ID=<server-id>   # right-click server → Copy Server ID (needs Developer Mode on)
   ```
4. Deploy via `setup_vm.sh` — the `discord` session starts automatically.

Without `DISCORD_BOT_TOKEN` the bot is silently disabled; webhooks for trade alerts and daily summaries still work normally.

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

Runs on a VPS (plain Python process under a virtualenv; Docker has been removed).

**Host:** `opc@148.116.94.154`  
**SSH key:** `~/.ssh/ssh-key-2026-05-31.key`  
**Repo path on VPS:** `~/Polymarket/`

```bash
# Interactive shell
ssh -i ~/.ssh/ssh-key-2026-05-31.key opc@148.116.94.154

# Run a command remotely without opening a shell
ssh -i ~/.ssh/ssh-key-2026-05-31.key opc@148.116.94.154 '<command>'

# Full redeploy (pull, deps, env hygiene, validate, restart all tmux sessions)
ssh -i ~/.ssh/ssh-key-2026-05-31.key opc@148.116.94.154 'bash ~/Polymarket/scripts/setup_vm.sh'
```

The three tmux sessions on the VPS:

| Session | Profile | What runs |
|---|---|---|
| `prod` | `PROFILE=prod` | copy_trade (live) |
| `paper` | `PROFILE=experimental` | insider_flow (paper) |
| `archive` | — | discovery price archiver |
| `discord` | — | Discord bot (all profiles, slash commands) |

```bash
# Attach to a session (Ctrl-b d to detach)
tmux attach -t prod
tmux attach -t paper
tmux attach -t archive
```

Persist `data/positions.db` and `.env` (5 `POLY_*` secrets) on the host — both are gitignored and must not be wiped between deploys.
