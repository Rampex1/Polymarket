# CLAUDE.md — Polymarket Copy-Trading Bot

## Claude Behavior

- After implementing changes, **commit automatically** (no need to ask).
- **Do not push** until the user explicitly says to.
- **Keep comments short.** Write one only when the code can't say it itself —
  a non-obvious constraint, a why, a gotcha that would otherwise be
  rediscovered the hard way. Never restate the line below it.

## Start with the README

`README.md` is the developer-facing doc and is **not duplicated here**:
architecture, repo map, setup + configuration, testing, deployment, and what
each Discord command reports. Read it first. This file covers what an agent
working *inside* the code needs on top of that — invariants, module
responsibilities, runtime wiring, and the DB schema.

The README was deliberately trimmed, so a few operational commands now live
only here:

```bash
PROFILE=<name> python main.py                   # run one profile's workers
python -m discovery.archive --once              # archiver: one pass (cron-friendly)
python -m discovery.archive --loop --every 3600 # archiver: long-running
```

## Invariants — don't break these

- **`PROFILE` is required.** No default profile, deliberately. Bare
  `python main.py` exits with an error naming the available profiles.
- **No global paper/live flag.** Mode is per-`[[algorithm]]` block. A single
  process can run live and paper algorithms side by side.
- **`allow_live = true` gates real money.** The loader rejects any
  `mode = "live"` block in a profile without it; only `prod.toml` sets it.
- **`name` is the DB partition key.** Renaming an algorithm orphans its
  bankroll and history. Keep it stable once set.
- **Knob schemas live in `algorithms/<type>/params.py`** — names, types,
  docs, and boot-time `validate()`. Never enumerate knobs in prose docs;
  they go stale. The dataclass is the source of truth.
- **Algorithm params are never read from env.** Env holds secrets and infra
  only. Behavior changes are TOML edits.
- **The params schemas carry no defaults.** Every field is required, so a
  profile block states every knob and an algorithm can never be half
  configured — `CopyTradeAlgorithm()` is a `TypeError`, and the loader
  rejects a block that omits a knob, naming what's missing. A block is
  therefore the complete answer to "what is this algorithm running?".
  Tests get their values from `copy_trade_params()` / `insider_flow_params()`
  in `tests/conftest.py`, deliberately separate from production.
- **One `.env`, shared by every profile.** Per-profile env files are not
  read and `setup_vm.sh` deletes any it finds. Don't reintroduce a
  `.env.<profile>` fallback: paper's safety comes from the `allow_live`
  gate, and a second env file only adds a way to shadow the real one.
- **The loader fails fast** on unknown keys, omitted keys, duplicate names,
  bad modes, and `validate()` violations. A typo in a TOML key must crash, never silently
  no-op — preserve that property when touching `profile_loader.py`.
- **Both order legs must poll a `delayed` response.** The CLOB matching
  engine is async: a small order returns `status='delayed'` with empty
  amounts and resolves seconds later. Parsing that as-is reports a no-fill —
  survivable on a BUY, but on a SELL it leaves a phantom position (ledger
  holds shares we sold, proceeds never credited, live reconciliation drifts).
  `_place_buy`/`_place_sell` are thin wrappers over one `_place_order` so
  the two legs cannot diverge on this again.
- **Never settle a market that is still trading.** A live favourite sits at
  0.97 for weeks undecided, and `fetch_resolution_price` is the last trade on
  an open book, not a resolution feed. `resolve_close_price` therefore gates
  its binary-price fallback on Gamma reporting the market closed, and returns
  `None` otherwise — the caller leaves the position open and a later sweep
  retries. Settling early books P&L off a live book, drifts the DB from the
  on-chain position, and writes a `signals.outcome` label that is write-once.
- **If an algorithm declares `LIVE` but no CLOB client can be built**, `main()`
  exits — it does **not** fall back to paper. Silently papering a live
  algorithm is worse than not starting: the operator believes real money is
  at work. Keep that guard; workers assume a live algo always has a client.

## Module map

```
main.py                   # Entry point — worker thread per algorithm, signal handling, shared threads
bot/
  config.py               # Infra only: API URLs, creds, DB path, webhook resolution, timezone, heartbeat interval
  profile_loader.py       # config/<profile>.toml → [Algorithm]; fail-fast validation
  reconciliation.py       # Diff bot DB vs on-chain positions (live only); logs + Discord alerts
  domain/                 # Side-effect-free shared vocabulary; re-exports nothing, one path per name
    algorithm.py          # Algorithm ABC + the AlgoParams protocol — the strategy contract
    intents.py            # What we decided: Open/Close/SettleIntent
    position.py           # What we hold in a market
    records.py            # What the APIs said: Trade, GlobalTrade
    mode.py               # Mode (paper/live) — no global switch, one per algorithm
  storage/                # Every SQLite table and the code that reads it
    db.py                 # Thread-local connections, WAL, schema on every connect
    ledger.py             # Ledger — positions, trade_log, daily_stats, paper_account
    signals.py            # Signal feature logging — training-data rows, outcome-labeled at settle
    runs.py               # Run provenance — resolved params + git sha per boot
  execution/              # Turning intents into fills; re-exports nothing
    runner.py             # Shared dispatch: risk check, slippage gate, CLOB orders (FAK/GTC), paper fills, DB writes, notify
    risk.py               # RiskManager — pre-trade caps from each algo's params; BUYs only
    fills.py              # Paper-exchange fills — no DB, notification, or strategy imports
    pricing.py            # Current price + the slippage gate
    settlement.py         # Resolved-market sweep + canonical close price; refuses anything still trading
    lots.py               # Attributed lots — closing one source unwinds only its share
  polymarket/             # Everything that talks to Polymarket
    api.py                # Raw HTTP reads — wallet lookup, trades, positions, prices, resolution
    gateway.py            # The boundary strategies depend on instead of the transport
  discord/                # Everything that talks to Discord
    webhook.py            # The fire-and-forget POST every sender shares
    alerts.py             # One message per trade event
    heartbeat.py          # The only scheduled message — liveness ping, no portfolio data
    messages.py           # Message rendering — markdown escaping, market URLs, feature lines
    threads.py            # Thread registry + lifecycle — (market_id, algo, paper) → thread_id, so a market's updates nest
    discord_bot.py        # Slash commands — a text builder per command, plus the gateway wiring (own process)
  logs/                   # Local log sinks
    setup.py              # Console at INFO + cumulative logs/<profile>/{debug,info,warn,error}.log, rotated daily, 14 kept
algorithms/
  __init__.py             # REGISTRY (type → classes) + lazy ENABLED via profile_loader (PEP 562)
  copy_trade/             # Mirror one target wallet, or a ranked cohort
    algorithm.py          # CopyTradeAlgorithm: poll → tier sizing → emit intents
    params.py             # CopyTradeParams — pure schema
    multi_leader.py       # Multi-leader event watcher + consensus-to-intent translation
    ranker.py             # Offline-testable confidence-adjusted wallet ranking
    watchlist.py          # SQLite-backed scored-wallet cohort, atomically replaced on refresh
    public_history.py     # Public-API ingestion for the ranked-copy history store (offline job)
  insider_flow/           # Copy suspicious fresh-wallet whale buys (no known target)
    algorithm.py          # InsiderFlowAlgorithm: /trades firehose → freshness filter → intents
    params.py             # InsiderFlowParams — pure schema
discovery/archive.py      # Price-history archiver (CLOB drops history at resolution — hoard it)
scripts/
  setup_vm.sh             # Zero-to-running VPS deploy; also what /restart invokes. Never run from CI — deploys are manual. Re-execs itself after the pull (SETUP_VM_REEXEC) so a deploy that changes this file still runs the new copy — keep that guard
  ssh_vm.sh               # SSH into the VPS
  run_discord_bot.py      # Standalone Discord bot — loads every profile, one token
  import_wallet_history.py            # Dune CSV → normalized resolved-bet history
  import_polymarket_wallet_history.py # Public-API seed for the same store (weaker, paper only)
  reset_paper_trade_db.py             # Wipe and reset paper trading state
  trading_account_summary.py          # Portfolio snapshot
```

## Runtime wiring

### Per-algorithm worker (`main.py:_run_worker`)

Each enabled algorithm gets a daemon thread with its own `Ledger`
(rows partitioned by `algo`), `RiskManager` built from that algorithm's
params, poll cadence, and paper bankroll (a per-algo row in
`paper_account`). Exceptions inside poll/dispatch are caught and logged so
one algorithm can't kill another; after `CRASH_ALERT_AFTER_N_ERRORS`
consecutive failures the worker posts a Discord instability alert.

The CLOB client is built **once** in `main`, and only if at least one
enabled algorithm is `Mode.LIVE`.

Live-only reconciliation runs at startup and then every
`RECONCILE_EVERY_N_POLLS` (30) polls — ~10 min at the default 20s cadence.
Failures only log; they never abort the worker.

### Profile-level threads (not per-worker)

Started once by `main`, aggregating all algorithms in the profile:
a liveness heartbeat and nothing else (`HEARTBEAT_INTERVAL_HOURS`,
default 6, `0` disables). It carries no portfolio data — deliberately, so
silence in the channel is the only signal it sends. Portfolio state is
answered on demand by the slash commands; `/summary` is the sole entry
point for a full summary, and it composes the text itself.

### Trade lifecycle

`poll()` classifies target activity into BUY → `OpenIntent` (top up to the
tier implied by the target's *total* holding), SELL → `CloseIntent` (resize
to the post-sell tier), MERGE → `CloseIntent(fraction=1.0)` (no slippage
gate), REDEEM → `SettleIntent`. `runner.dispatch()` then does risk check →
slippage gate → order or paper fill → DB write → notify.

Non-obvious behavior, easy to "fix" by mistake:

- `fetch_target_position_value` retries until the API reflects the BUY it
  just saw. Without it a stale read under-sizes our top-up.
- SELL carries the target's fill price, so the slippage gate is live on
  partial closes — a drifted market skips the resize this tick and re-tiers
  on the next SELL.
- Positions are keyed by `market_id`, not outcome, so YES and NO in one
  market would collide. Safe only because we buy the side the target bought.
- Target holding both sides: position value sums all rows, and a SELL
  decrements the cache whichever side it was. MERGE covers the usual exit.

### insider_flow

Detects the documented insider fingerprint: **fresh wallets making large
first bets at long odds.** Polls the platform-wide Data-API `/trades`
firehose (cash-filtered server-side), screens markets against Gamma
category/tags (sports = gambling, not signal) and a time-value gate (must
resolve soon and out-earn an index fund for the wait — unknown end date
fails closed), then vets each candidate wallet's age/history via one
`/activity` page (unverifiable wallets are *not* copied — fail closed).
Survivors are buffered for a window, ranked by conviction score, and only
the top N are copied. Exits at market resolution via a periodic Gamma
sweep. Its `max_slippage` default (0.10) is deliberately wider than
copy_trade's — these signals move fast. Defaults are sized for a **~$20
prod bankroll**; scale via the profile TOML when capital grows.

## Environment variables (`bot/config.py`)

| Variable | Notes |
|---|---|
| `PROFILE` | Selects `config/<name>.toml`. Required. |
| `POLY_PRIVATE_KEY` / `POLY_FUNDER_ADDRESS` / `POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE` | Live trading only. `POLY_FUNDER_ADDRESS` is the Polymarket **proxy wallet** (from the profile URL) — without it orders sign correctly but debit the wrong account. |
| `DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` | Slash-command bot. Unset → bot silently disabled, webhooks unaffected. Guild ID gives instant command registration vs. ~1h global. |
| `DB_PATH` | Default `data/positions.db`. |
| `HEARTBEAT_INTERVAL_HOURS` | Default 6; `0` disables the liveness ping. |
| `TIMEZONE` | Daily-summary rollover (default `America/New_York`). |
| `POLY_SIGNATURE_TYPE` | Default 3 (smart-wallet EIP-1271). |

### Webhook routing

Two independent paths, no global fallback between them:

- **Trade alerts** come from each algorithm's own `webhook_url` param in
  the profile TOML, which `validate()` requires — a block without one is a
  boot-time `ProfileError`, not a silently muted algorithm.
- **Profile-level messages** (heartbeat, `/summary`) come from
  `config/webhooks.toml` via `resolve_summary_webhook(profile)`, matching a
  block with `type = "summary"` and `profile = "<name>"`.

## Database schema (SQLite)

Thread-local connections, WAL mode. Rows are partitioned by an `algo`
column so multiple algorithms share one DB without collisions.

- `positions` — open positions, PK `(market_id, paper, algo)`
- `trade_log` — all executed trades (BUY/SELL/REDEEM), tagged with `algo`
- `daily_stats` — per-`(date, algo)` realized P&L
- `paper_account` — virtual cash balance, PK `algo`
- `position_lots` — attributed fills behind an aggregated position; `source` is an opaque key (copy_trade passes a leader wallet)
- `signals` — one row per dispatched `OpenIntent` (executed or skipped): raw `features` JSON at signal time, `outcome`/`pnl_usdc` backfilled at settlement. Training data — log raw observables, never derived scores.
- `discord_threads` — `(market_id, algo, paper)` → Discord thread id, so a market's updates nest under its opening message
- `runs` — one row per worker boot: resolved params JSON, profile, git sha. Written on every boot; nothing reads it yet. Kept so the config behind a stretch of results stays recoverable after the fact.

Schema is created by `CREATE TABLE IF NOT EXISTS` on every connect. There
is no migration path — a database predating the multi-algorithm layout
(`algo` columns, `(market_id, paper, algo)` PKs) will not work.

The archiver uses a **separate** DB (`data/discovery_archive.db`):
`price_history` and `tracked_markets`, both with natural PKs.

**Never copy one `discovery_archive.db` over another** — laptop and VPS each
hold history the other lacks, the public API won't re-serve it once markets
resolve, and an overwrite is silent. Merge instead; the natural PKs make
`INSERT OR IGNORE` exact:

```bash
# upload under a temp name, then on the target machine:
sqlite3 discovery_archive.db "
  ATTACH 'other_archive.db' AS other;
  INSERT OR IGNORE INTO price_history   SELECT * FROM other.price_history;
  INSERT OR IGNORE INTO tracked_markets SELECT * FROM other.tracked_markets;"
```

## External APIs

| API | Base URL | Used for |
|---|---|---|
| Gamma | `https://gamma-api.polymarket.com` | Username → proxy wallet (`/profiles`); market resolution + tags (`/markets`) |
| Data | `https://data-api.polymarket.com` | Trade history (`/activity`), positions (`/positions`), platform firehose (`/trades`) |
| CLOB | `https://clob.polymarket.com` | Order placement, last-trade price, `/prices-history` |

## Discord bot

Runs as a **standalone process** (`scripts/run_discord_bot.py`, tmux
session `discord`) — *not* a thread inside `main.py`. It loads every
available profile, so one bot and one token cover all of them. If a worker
crashes the bot stays up; if the bot crashes the workers keep trading.

| Command | Description |
|---|---|
| `/status` | All algorithms: mode, exposure, today's P&L |
| `/positions [algo]` | Open positions, optionally filtered by algo |
| `/pnl` | Realized P&L + exposure per algo and combined |
| `/summary` | Post a portfolio summary now — there is no scheduled one |
| `/restart` | git pull + `setup_vm.sh` on the VPS — **admin permission required** |

One-time setup: [discord.com/developers](https://discord.com/developers) →
New Application → Bot → Reset Token; OAuth2 URL Generator with scopes
`bot` + `applications.commands` and permissions `Send Messages` +
`Use Slash Commands`; then put `DISCORD_BOT_TOKEN` and `DISCORD_GUILD_ID`
in the VPS `.env` and redeploy.

## Test conventions (`tests/conftest.py`)

- Use a **real** SQLite DB via tempfile — no DB mocking. The real engine
  catches PRAGMA/index/migration bugs a mock would hide.
- Use real `RiskManager`, `Ledger`, `Trade`. **The only thing
  stubbed is the HTTP boundary** (Polymarket API).
- Each test gets a fresh DB; ledger fixtures are bound to the
  `copy_trade` algo namespace.
- `conftest.py` puts the repo root on `sys.path`, which depends on
  `pytest.ini` staying at the repo root (it sets rootdir).
