# CLAUDE.md — Polymarket Copy-Trading Bot

## Claude Behavior

- **Never modify anything under `bot/` without explicit permission.** Ask
  first, every time, and say what the change would be and why nothing in
  `algorithms/` can do it instead. `bot/` is shared infrastructure — the
  ledger, execution, gateway, loader, and Discord plumbing that every
  strategy depends on — so a change there to suit one strategy silently
  changes them all. This holds even when editing `bot/` looks like the
  shorter diff.
- **copy_trade is the user's code.** Its logic lives in
  `algorithms/copy_trade/`, including the scripts that operate on it, which
  are package modules run with `python -m`. Do not scatter copy-trade logic
  into `scripts/` or `bot/`. Tests are the exception and belong in `tests/`.
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
python -m algorithms.insider_flow.archive --once            # archiver: one pass
python -m algorithms.insider_flow.archive --loop --every 3600
python -m algorithms.copy_trade.cohort --discover 40 --activate <algo>   # build/seat a cohort
python -m algorithms.copy_trade.report cohort.txt           # read-only consensus report
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
- **`algorithms/<type>/params.py` is the whole config surface** — names,
  types, values, docs, and boot-time `validate()`. Never enumerate knobs in
  prose docs; they go stale. The dataclass is the source of truth.
- **The trading path reads Polymarket through `MarketDataGateway`,** never
  `polymarket.api` directly — strategies take one in their constructor,
  `pricing`/`settlement` take an optional `market_data=` defaulting to
  `DEFAULT_MARKET_DATA`. That keeps the external dependency visible in a
  signature and swappable without monkeypatching. `insider_flow/archive.py`
  is deliberately outside it: an offline job with its own database.
- **Algorithm params are never read from env.** Env holds secrets and infra
  only.
- **Knob values live only in `algorithms/<type>/params.py`.** One source of
  truth per knob. A profile TOML declares *what runs* — `type`, `name`,
  `mode` — and nothing else; an `[algorithm.params]` block is a boot error,
  not a silent no-op. Tuning is therefore a code edit and a redeploy, and
  two blocks of the same type inside one profile differ **only** through a
  `VARIANTS[name]` entry in that same params.py — an A/B lever, not a
  general tuning channel. Values still live in one file, and an unknown knob
  in a variant raises rather than silently running the control.
- **One `.env`, shared by every profile.** Per-profile env files are not
  read and `setup_vm.sh` deletes any it finds. Don't reintroduce a
  `.env.<profile>` fallback: paper's safety comes from the `allow_live`
  gate, and a second env file only adds a way to shadow the real one.
- **The loader fails fast** on unknown keys, omitted keys, duplicate names,
  bad modes, and `validate()` violations. A typo in a TOML key must crash, never silently
  no-op — preserve that property when touching `profile_loader.py`.
- **Every ledger write goes inside `with conn:`.** Recording a trade touches
  positions, trade_log, and (in paper) paper_account; they must land together.
  The connection is thread-local and reused, and `main.py`'s poll loop
  swallows exceptions and keeps running — so without the context manager an
  exception mid-write leaves statements pending and the *next* trade's commit
  flushes a half-recorded one (a position with no trade_log row and no cash
  debit). `with conn` rolls back instead. Never add a bare `conn.commit()`.
- **Both order legs must poll a `delayed` response.** The CLOB matching
  engine is async: a small order returns `status='delayed'` with empty
  amounts and resolves seconds later. Parsing that as-is reports a no-fill —
  survivable on a BUY, but on a SELL it leaves a phantom position (ledger
  holds shares we sold and the proceeds are never credited).
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
  caches.py               # SeenRing (bounded dedupe) + TargetHoldingCache — in-memory, no I/O
  polymarket/             # Everything that talks to Polymarket
    api.py                # Raw HTTP reads — wallet lookup, trades, positions, prices, resolution
    gateway.py            # MarketDataGateway — the injectable read boundary
  discord/                # Everything that talks to Discord
    webhook.py            # The fire-and-forget POST every sender shares
    alerts.py             # One message per trade event
    heartbeat.py          # The only scheduled message — liveness ping, no portfolio data
    messages.py           # Message rendering — markdown escaping, market URLs, feature lines
    threads.py            # Thread registry + lifecycle — (market_id, algo, paper) → thread_id, so a market's updates nest
    discord_bot.py        # Slash commands — a text builder per command, plus the gateway wiring
    __main__.py           # `python -m bot.discord` — loads every profile, one token, own process
  logs.py                 # Console at INFO + cumulative logs/<profile>/{debug,info,warn,error}.log, rotated daily, 14 kept
algorithms/
  __init__.py             # REGISTRY (type → classes) + lazy ENABLED via profile_loader (PEP 562)
  copy_trade/             # Mirror one target wallet, or trade a cohort's consensus
    algorithm.py          # CopyTradeAlgorithm: poll → tier sizing → emit intents
    params.py             # CopyTradeParams — pure schema
    consensus.py          # Pure grouping: cohort positions → markets they agree on. No I/O, so thresholds sweep offline
    engine.py             # ConsensusEngine — snapshots the cohort, screens, emits intents
    cohort.py             # `python -m` job: discover wallets, reconstruct history, rank, --activate a cohort
    report.py             # `python -m` job: read-only consensus report. Places no orders
    history.py            # Pure reconstruction of a wallet's resolved bets from /activity + /positions
    ranker.py             # Offline-testable confidence-adjusted wallet ranking; reads/writes wallet_resolved_bets
    watchlist.py          # SQLite-backed scored-wallet cohort, atomically replaced on refresh
  resolution_carry/       # Buy near-certain outcomes, hold to resolution, collect the residual. Registered, running paper in experimental.toml. See PLAN.md
    algorithm.py          # ResolutionCarryAlgorithm: paged Gamma scan → screen → OpenIntent; settle sweep for exits
    params.py             # ResolutionCarryParams — pure schema
    screen.py             # Pure: market rows → ranked, diversified candidates. No I/O, so the funnel sweeps offline
    seed.py               # `python -m` job: seed the price archive with markets resolving soon
    calibration.py        # `python -m` backtest of the calibration curve against discovery_archive.db
  insider_flow/           # Copy suspicious fresh-wallet whale buys (no known target)
    algorithm.py          # InsiderFlowAlgorithm: /trades firehose → freshness filter → intents
    params.py             # InsiderFlowParams — pure schema
    archive.py            # Price-history archiver (CLOB drops history at resolution — hoard it); own DB, own process
    research/             # Plans and notes behind this strategy
scripts/
  setup_vm.sh             # Zero-to-running VPS deploy; also what /restart invokes. Never run from CI — deploys are manual. Re-execs itself after the pull (SETUP_VM_REEXEC) so a deploy that changes this file still runs the new copy — keep that guard
  reset_paper_trade_db.py             # Wipe and reset paper trading state
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

### Profile-level threads (not per-worker)

Started once by `main`, aggregating all algorithms in the profile:
a liveness heartbeat and nothing else (`HEARTBEAT_INTERVAL_HOURS`,
default 6, `0` disables). It carries no portfolio data — deliberately, so
silence in the channel is the only signal it sends. Portfolio state is
answered on demand by the slash commands; `/summary` is the sole entry
point for a full summary, and it composes the text itself.

### copy_trade consensus mode

A non-empty `watchlist_candidate_wallets` swaps `CopyTradeAlgorithm`'s
single-target feed for `ConsensusEngine`. Every
`snapshot_interval_seconds` it pulls each cohort wallet's standing positions
(one call per wallet, threaded), groups them by `(market, outcome)` via
`consensus.find_consensus`, and opens where support minus opposition clears
the bar. Consensus is read off *standing positions*, not entry events, so
agreement accumulated days apart still counts and a missed poll costs
nothing.

Non-obvious behavior:

- **Group by `asset_id`, never by market.** Five wallets on YES plus five on
  NO is maximum disagreement; grouping by market alone scores it as ten-way
  consensus. Only the winning side is ever opened, which preserves the
  `positions` PK assumption below.
- **Both price ends are dead ends.** `consensus_max_price` drops finished
  markets still sitting at 1.00 with `redeemable` false;
  `consensus_min_price` drops the fossils — a cohort down 98% is holding a
  position nobody bothered to sell, not an opinion.
- **`consensus_max_drift` is the adverse-selection gate.** The cohort's edge
  is in their entry price; once a market has run well past their cost basis,
  copying it buys their exit liquidity.
- **Being at tier is the dedupe.** A fully-sized market yields nothing on
  every later snapshot, so there is no `SeenRing`.

Exits are decay-first, settlement-second. When support for a held asset falls
to `consensus_exit_leaders` or below, the position is fully closed with
`signal_price=0` (gate off, as on a MERGE — the reason to hold is gone and a
slippage check could only strand us). Anything the cohort holds to the end is
closed by the shared settle sweep instead. Three guards that look optional and
are not:

- **`consensus_exit_leaders` must sit below `consensus_min_leaders`.** Without
  the band a position churns open and closed on one leader trimming.
  `validate()` enforces it.
- **`snapshot_min_responders` is data-loss protection.** `user_positions`
  returns `[]` on a failed fetch by design, so a Data-API wobble reads as the
  whole cohort abandoning everything at once. Below that fraction of
  responders, decay exits are skipped entirely for the snapshot.
- **A market past its end date is settlement's, not decay's.** Cohort rows go
  `redeemable` at resolution and drop out of the snapshot, so support reads
  zero for a market that is merely resolving — selling into that books P&L off
  a dead price. Positions with no `asset_id`, or whose Gamma lookup failed,
  are skipped for the same reason: never act on an unverifiable reading.

### The paper A/B on category screening

`config/experimental.toml` runs two `copy_trade` blocks over the same 21-wallet
cohort, differing only in `exclude_categories` via `VARIANTS`:

| name | sports |
|---|---|
| `copy_trade_paper` | excluded (control) |
| `copy_trade_sports_paper` | kept (treatment) |

The screen was imported from insider_flow as an assumption and has never been
tested against our own P&L, while it currently drops ~86% of consensus
signals. Every `OpenIntent` logs `market_category` in its `signals` features
row and gets `outcome`/`pnl_usdc` at settlement, so split realized P&L by
category and algo to settle it. Both arms share insider_flow's webhook for
now; each message carries its `display_name`.

### Building the cohort (`python -m algorithms.copy_trade.cohort`)

Batch, not a worker thread. The ranker consumes *resolved* bets, so its input
only moves as markets settle — days to weeks. Ingestion stays out of the poll
loop so a slow crawl can never delay a snapshot. The engine reads
`active_wallets()` every snapshot, so `--activate` propagates within one
snapshot interval with no redeploy and no refresh timer.

Every bet is anchored to a BUY in the `/activity` window; `/positions` is only
a lookup for "did this resolve, and which way". Reading them as two independent
sources is what goes wrong, in two ways:

- **Each covers one outcome.** `/positions` keeps a resolved position only
  until it is redeemed — winners get claimed and vanish, losers have nothing to
  claim and sit forever (measured: 227/227 and 498/498 `redeemable` rows were
  losses). REDEEM rows are the mirror image, all winners.
- **They run on different clocks.** A REDEEM is stamped when the wallet got
  round to claiming — often months later, usually in bursts — while a loss can
  only be dated by its market's end. Reconciling that with a time window
  overcorrects: one pass produced wallets at 100-0 and 1266-1.

Three API shapes that bite, all verified against live data:

- `/activity` 400s past `offset + limit > 5500`, so a wallet's readable history
  stops there. `fetch_activity` returns **None** on failure, not `[]` —
  flattening the two lets a failed page read as "history exhausted".
- `/positions` caps at 500 rows per page and **pages with `offset`**. Wins come
  from up to 5500 activity rows, so reading one page of losses hands the ranker
  a wallet that mostly wins.
- `/positions` default ordering buries live positions under years of worthless
  unredeemed losers — 2 live rows in the first page against 40 with
  `sortBy=CURRENT&sortDirection=DESC`. `fetch_user_positions` always sorts, and
  the consensus snapshot depends on it.

**Known limitation:** a `ResolvedBet` only exists for a bet held to resolution,
so the ranker sees 38–49% of a typical wallet's bets and systematically
flatters anyone who exits losers early. Absolute edge numbers are optimistic;
treat the ranking as relative order within the pool, not an expected return.
Firehose discovery adds its own survivorship — wallets placing $20k tickets
today are disproportionately ones that have been winning.

`WatchlistRepository.refresh` activates nothing unless `persistence_passes`
(early winners still winning late) **and** a wallet's `edge_lower_bound > 0`.
Rank order alone would seat the least-bad wallet in a weak pool.

### Trade lifecycle

Single-target mode. `poll()` classifies target activity into BUY → `OpenIntent` (top up to the
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
prod bankroll**; scale in `algorithms/insider_flow/params.py` when capital grows.

### resolution_carry

Buys the top of the book (`bestAsk` in 0.95–0.985, resolving within a day)
and holds to settlement, so the premise is that the price is *correct* — not
that we know better.
Each poll pages Gamma's volume-ordered open markets inside the resolution
window, screens on price band → spread/liquidity → time value → category →
diversification, ranks survivors by annualised return, and opens a flat
stake. Exits are the shared settle sweep only.

Non-obvious behavior, all deliberate:

- **Sports is not required; crypto is excluded** (`require_sports = False`,
  `exclude_categories = ("crypto", "esports")`). A "will BTC be above X at
  4pm" market is a live price, not a decided outcome; a best-of-three at 0.95
  is one teamfight from 0.40. Labels are reliable: 130/130 crypto and 94/94
  esports markets carry theirs, and 0 of 300 markets carry none.
- **`require_in_play = True` — the gate that encodes the thesis.** 0.95
  before kickoff is a forecast; 0.95 with the game underway is a scoreboard.
  Only the second is an outcome awaiting paperwork. Uses Gamma's
  `gameStartTime` (present on 1,871/2,100 in a one-day window); markets
  without it are not games and are rejected. `startDate` is NOT a fallback —
  every market has one, so it would silently disable the gate. Every other strategy screens sports out
  because efficient pricing kills a forecasting edge; here efficiency is the
  product. Sports was required at first, on the assumption that only a game
  has a knowably certain resolution time — measured and false: non-sports
  markets honoured their stated end date 983/984 against 92% for sports
  (games get postponed), so the one-day cap already does that job.
  `require_sports` stays as a switch and `market_category` is logged on
  every signal, so the arms can be settled on realized P&L.
- **`min_hours_to_resolution = 0`, unlike every sibling.** The trade is a
  match decided on the pitch sitting at 0.97 while it waits to settle —
  minutes out, not hours. A 6h floor excluded exactly that and left a
  universe of one sports market. The gate still rejects a market past its
  end date (a dead book with no carry left), and `validate()` refuses a
  negative value.
- **`poll_interval_seconds = 15`, from a measurement.** Across 4,765
  archived transits through the band on tokens that finished ≥0.99, 99%
  lasted a single one-minute sample. The band is open for about a minute, so
  a 60s poll misses transits outright whenever one falls between two polls;
  15s gives ~3-4 looks. A scan is 21 pages / ~6.5s, so the effective
  cycle is ~21s and a poll never overlaps its own scan.
- **`max_ask_spread = 0.05`, wider than it looks like it should be.** In-play
  books run wider than pregame ones, and at 0.02 nothing live ever qualified.
  The spread is not a cost here — positions are held to resolution, never
  sold back across the bid — so this caps how far above the *mid* we will
  pay, not friction. Watch that premium: a 0.069 spread put the ask 3.4c over
  mid on a trade returning 2.1c.
- **`order_type = "limit"`.** One tick of slippage is a quarter of the
  return. Non-fills are the accepted cost; whether a GTC at the ask reliably
  fills is the open question paper exists to answer.
- **The per-event cap is the risk control**, not tidiness. At 0.98 the loss
  is 49× the win, so twenty legs of one event is one position with twenty
  times the size. `screen.select` applies it to a single poll's own picks as
  well as to open positions. `max_positions_per_category` is deliberately
  inert (20): every sports market's primary Gamma label is `sports`, so a
  real value there caps total positions rather than capping a theme.
- **Being at size is the dedupe.** A held market yields nothing on later
  scans, so there is no `SeenRing`.
- **No stop-loss.** A stop realises exactly the losses the strategy exists to
  absorb. New lows are logged (`_note_lows`) so the question can be settled
  with data instead of intuition.

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
  `algorithms/<type>/params.py`, which `validate()` requires — an empty one
  is a boot-time `ProfileError`, not a silently muted algorithm.
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
- `wallet_resolved_bets` — reconstructed wallet history, the ranker's only input. Natural PK `(wallet, resolved_at, entry_price, outcome)` makes re-ingestion idempotent; a duplicate would silently double-weight that wallet. Written only by `scripts/rank_wallets.py`
- `wallet_score_runs` / `wallet_scores` — one row per ranking run and its scores, kept so a cohort choice stays explicable after the fact
- `copy_watchlist` — `(algo, wallet)` → active/demoted. What `ConsensusEngine` reads each snapshot
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

Runs as a **standalone process** (`python -m bot.discord`, tmux
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
