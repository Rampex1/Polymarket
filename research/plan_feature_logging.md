# Implementation Plan — Data Directory, Signal Feature Logging, Sizing Math

Date: 2026-06-10
Status: approved scope from architecture discussion (integration + cleanup)

## Why these three, in this order

Everything else on the roadmap (confidence-weighted sizing, Build 2 scoring,
new detectors) consumes **labeled training data**: features captured at signal
time, joined to the resolved outcome. That data cannot be reconstructed later —
wallet balances change, books move, the firehose window slides. Every week the
paper trader runs without feature capture is training data permanently lost,
the same way unarchived price history is lost. So feature logging ships before
any detector or sizing work.

The data/ directory move is a 30-minute cleanup that must land **before** VPS
deployment (so the deploy starts clean), and the sizing module is pure math
needed by the eventual confidence model — building it now costs little and
lets offline analysis use it immediately.

Explicitly **out of scope** for this pass: new detectors (holders sweep,
clusters, funding provenance), Build 2 scorer, the bot/api package split,
removing the Trade adapter, deployment automation.

---

## Workstream 1 — `data/` directory for all SQLite files

### Problem
`positions.db` and `discovery_archive.db` (+ WAL/SHM sidecars) land in the
repo root. Clutter, and backup/rsync of a deployment means cherry-picking
files.

### Changes
1. `bot/config.py`
   - `DB_PATH` default changes from `positions.db` → `data/positions.db`,
     via a `_default_db_path()` function with a **legacy fallback**: if the
     env var is unset AND `./positions.db` exists AND `data/positions.db`
     does not, keep using `./positions.db` (log a warning). This protects the
     VPS: a `git pull` + restart must not silently start a fresh DB while the
     real one sits in the old location.
2. `bot/db.py`
   - `get()` creates the parent directory of `config.DB_PATH` before
     `sqlite3.connect` (connect fails on missing dirs).
3. `discovery/archive.py`
   - `default_db_path()` default changes to `data/discovery_archive.db`,
     same legacy-fallback rule.
   - `connect()` creates parent dirs.
4. `.gitignore`: add `data/`; the specific `discovery_archive.db*` entry
   stays for the legacy location.
5. Local machine: move the existing `discovery_archive.db*` files into
   `data/` (so the fallback doesn't trigger here).

### Tests
- `connect()`/`get()` create nested parent dirs (point at `tmp/sub/dir/x.db`).
- `default_db_path()` returns `data/discovery_archive.db` when env unset and
  no legacy file exists.
- Legacy fallback: with cwd containing `./positions.db` and no
  `data/positions.db`, the default resolves to the legacy path.

---

## Workstream 2 — Signal feature logging (the flagship)

### Design principle
**Log raw observables, not derived scores.** Derivations (Kelly-implied
belief, edge) can be recomputed offline forever; raw inputs cannot be
re-observed. Feature capture must NEVER block or fail the signal path — a
missing feature is `None`, an enrichment fetch failure is silently absent.

### Data model — new `signals` table (bot/db.py `_init_schema`)
```sql
CREATE TABLE IF NOT EXISTS signals (
    signal_id   TEXT NOT NULL,
    algo        TEXT NOT NULL,
    paper       INTEGER NOT NULL DEFAULT 1,
    ts          INTEGER NOT NULL,          -- when the runner saw the intent
    market_id   TEXT,
    asset_id    TEXT,
    question    TEXT,
    signal_price REAL,                     -- odds at signal time
    usdc_amount REAL,                      -- what we tried to spend
    features    TEXT NOT NULL DEFAULT '{}',-- JSON dict of raw observables
    executed    INTEGER NOT NULL DEFAULT 0,-- did an order actually fill
    skip_reason TEXT,                      -- risk / slippage / no-fill / ...
    outcome     REAL,                      -- close price at resolution (NULL until labeled)
    outcome_ts  INTEGER,
    pnl_usdc    REAL,                      -- realized P&L if we held it
    PRIMARY KEY (signal_id, algo)
);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(algo, market_id);
```
`CREATE TABLE IF NOT EXISTS` in `_init_schema` is sufficient — no `_migrate`
step needed for a brand-new table.

### New module: `bot/signals.py` (stateless functions, runner style)
- `record(algo_name, intent, paper, executed, skip_reason=None) -> None`
  Upsert by `(signal_id, algo)`. Re-dispatch of the same signal updates
  `executed`/`skip_reason` rather than duplicating. Serializes
  `intent.features` to JSON. Swallows + logs all exceptions (never breaks
  dispatch).
- `label_outcomes(algo_name, market_id, close_price, pnl_usdc, paper) -> int`
  Sets `outcome`, `outcome_ts`, `pnl_usdc` on all **unlabeled** rows for
  `(algo, market_id, paper)`. Returns rows labeled. Called from the settle
  path. Idempotent (only touches `outcome IS NULL` rows).
- `unlabeled_market_ids(algo_name, paper) -> list[str]`
  For future batch backfill of signals we skipped (no position → no settle
  intent → labeled later by a discovery batch job). v1 ships the query;
  the batch labeler itself is future work.

### Intent change: `bot/algorithm.py`
- `OpenIntent` gains `features: dict = field(default_factory=dict)`.
  Optional; algorithms that don't populate it lose nothing.

### Runner hooks: `bot/runner.py` `_handle_open`
Record exactly once per dispatch at the terminal point of each path:
- risk-blocked        → `record(..., executed=False, skip_reason="risk: <reason>")`
- slippage-skipped    → `record(..., executed=False, skip_reason="slippage")`
- no fill             → `record(..., executed=False, skip_reason="no fill: <reason>")`
- filled + recorded   → `record(..., executed=True)`

`_handle_settle` after `record_sell`: call
`signals.label_outcomes(algo.name, market_id, close_price, pnl, paper)`.
This labels every algorithm's signals for free since settle is shared.

Counterfactual note (v1 limitation): signals that never became positions
(risk-blocked etc.) get no settle intent, so they stay unlabeled until the
future batch labeler runs. They are still recorded — that's the point.

### Feature capture: `algorithms/insider_flow/algorithm.py`
Populate `OpenIntent.features` with raw observables already in hand
(zero new blocking calls except one optional /value fetch):
- `odds` — row.price
- `cash_usdc`, `shares` — observed trade economics
- `trade_count`, `activity_count` — from the freshness stats (already fetched)
- `wallet_age_seconds` — now − oldest_ts (None if no history)
- `detect_latency_seconds` — now − row.timestamp (signal staleness)
- `hour_utc` — time-of-day pattern
- `wallet` — for offline joins (provenance, CLV once Build 2 exists)
- `portfolio_value_usdc` — from new `fetch_wallet_value` (None on failure;
  enables offline Kelly inversion: bet_fraction ≈ cash / (value + cash))

Implementation detail: `_wallet_is_fresh` currently returns bool; it needs to
also expose the stats it fetched so features don't trigger a second lookup.
Refactor to `_vet_wallet(wallet) -> Optional[dict]` returning the stats dict
when fresh, None when rejected/unverifiable. The TTL verdict cache keeps
working (cache the stats dict instead of the bool).

### New fetcher: `fetch_wallet_value(address) -> Optional[float]`
GET `{DATA_API}/value?user=<addr>`. Defensive parse (list-of-dicts or dict),
returns float USD value of open positions or None on any failure. Same
log-and-return-None contract as the rest of the module.

### Tests
- `tests/test_signals.py` (new):
  - record + read back: JSON features roundtrip, executed flag, skip_reason
  - upsert: same (signal_id, algo) re-recorded updates, doesn't duplicate
  - label_outcomes: labels only unlabeled rows of that algo+market+paper;
    second call is a no-op; other algos/markets untouched
  - record never raises on bad input (e.g. unserializable feature value →
    logged, row still written with sanitized features or skipped silently)
- `tests/test_runner.py` additions:
  - dispatch of filled OpenIntent → signals row executed=1
  - risk-blocked dispatch → executed=0, skip_reason starts "risk:"
  - slippage-skip → executed=0, skip_reason="slippage"
  - settle → outcome + pnl labeled on that market's signal rows
- `tests/test_insider_flow.py` additions:
  - emitted intent carries features with expected keys/values
    (odds, cash_usdc, trade_count, wallet_age_seconds, wallet)
  - /value failure → portfolio_value_usdc is None, intent still emitted
  - vetting still single network call per wallet (cache test still passes)
- `tests/test_fetcher.py` additions:
  - fetch_wallet_value parses list + dict shapes, None on failure

---

## Workstream 3 — `bot/sizing.py` (pure Kelly math)

Used immediately by offline analysis of logged features; later by
confidence-weighted sizing. No bot wiring in this pass.

For a binary market at price `q` (cost per share, win pays 1):
- `kelly_fraction(p, q) -> float` — f* = (p − q) / (1 − q), clamped to [0, 1];
  0 when p ≤ q (no edge). Guards: q outside (0, 1) → ValueError.
- `implied_belief(bet_fraction, q) -> float` — inverse: p = q + f·(1 − q),
  clamped to [q, 1]. Lower bound on belief (bettors use fractional Kelly).
- `edge(p, q) -> float` — p − q.
- `stake(p, q, bankroll, kelly_scale=0.25, cap=None) -> float` —
  fractional-Kelly stake with optional absolute cap, never negative.

### Tests (`tests/test_sizing.py`)
- Known values: p=0.6, q=0.2 → f*=0.5; implied_belief(0.5, 0.2)=0.6
  (roundtrip property: implied_belief(kelly_fraction(p,q), q) == p)
- No edge: p ≤ q → fraction 0, stake 0
- Clamps: f > 1 input to implied_belief → 1.0 ceiling; p=1 → full Kelly 1.0
- Guards: q=0, q=1, q<0 → ValueError
- stake: kelly_scale and cap both respected; bankroll 0 → 0

---

## Execution order
1. Tests for all three workstreams (this is the TDD contract)
2. Workstream 1 (data dir) — smallest, unblocks deploy
3. Workstream 3 (sizing) — pure, no dependencies
4. Workstream 2 (signals) — db schema → signals module → intent field →
   runner hooks → insider_flow capture → fetch_wallet_value
5. Full suite green, logical commits

## Risks
- **VPS data orphaning** from the DB_PATH default change → mitigated by the
  legacy fallback + warning log; deployment checklist: either move the file
  into data/ or set DB_PATH explicitly.
- **Signal table growth**: one row per signal, not per poll — InsiderFlow
  produces a handful/day. Negligible.
- **/value endpoint shape uncertainty**: defensive parse, None on surprise,
  feature is optional by design.
