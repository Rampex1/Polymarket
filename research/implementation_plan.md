# Implementation Plan — InsiderFlow Detector + Price Archiver (Build 1)

Scope = items 1 + 2 of the build order in `discovery_plan.md`: the live
suspicious-flow detector (Detector B) and the price-history archiver, plus the
fetcher primitives both need. The statistical discovery pipeline (Detector A)
is Build 2 — it depends on nothing here and gets more valuable as the archiver
accumulates data.

Method: TDD. Tests are written first against the contracts below, then the
implementation makes them pass. Conventions follow the existing suite: real
SQLite via tmp_path, real tracker/risk, stub ONLY the HTTP boundary
(`monkeypatch` on `bot.fetcher` functions / `fetcher.SESSION.get`).

---

## Component 1 — `bot/fetcher.py` + `bot/models.py` additions (shared primitives)

### `bot/models.py`: new `GlobalTrade` dataclass
Parsed row from the platform-wide Data-API `/trades` firehose. Distinct from
`Trade` (the per-wallet /activity shape): firehose rows carry the trader's
wallet and shares+price instead of usdcSize.

```python
@dataclass
class GlobalTrade:
    tx_hash: str
    wallet: str          # proxyWallet of the trader
    side: str            # "BUY" | "SELL"
    price: float
    shares: float        # `size` field — token units, NOT usd
    cash_usdc: float     # shares * price, computed at parse time
    market_id: str       # conditionId
    asset_id: str        # token id
    timestamp: int
    title: str = ""
    outcome: str = ""
    trader_name: str = ""   # name or pseudonym, display only
```

### `bot/fetcher.py`: three new functions (pure additions, no behavior change)

1. `fetch_global_trades(min_cash_usdc: float, limit: int = 100) -> list[GlobalTrade]`
   - GET `{DATA_API}/trades?filterType=CASH&filterAmount=<min>&limit=<limit>`
   - `_parse_global_trade(row)` helper; rows missing
     tx/conditionId/proxyWallet/asset or with price/size ≤ 0 → dropped.
   - Failure → `[]` (same contract as `fetch_recent_trades`).

2. `fetch_wallet_stats(address: str, max_rows: int = 100) -> Optional[dict]`
   - GET `{DATA_API}/activity?user=<addr>&limit=<max_rows>` (newest-first).
   - Returns `{"trade_count": int, "activity_count": int, "oldest_ts": int, "capped": bool}`.
     `capped=True` when the page is full (wallet has ≥ max_rows activities —
     by definition not fresh; callers short-circuit on it).
   - Failure → `None` (callers treat as "can't verify" and fail CLOSED).

3. `fetch_price_history(token_id, fidelity=60, interval="max", start_ts=None, end_ts=None) -> Optional[list[dict]]`
   - GET `{CLOB_API}/prices-history`. Returns the `history` list (`[{t,p},…]`),
     `[]` when the API legitimately returns no points (old resolved market),
     `None` on HTTP failure (caller distinction: skip vs retry).

4. `fetch_top_markets(closed: bool, limit: int = 50, end_date_min: str = None, end_date_max: str = None) -> list[dict]`
   - GET `{GAMMA_API}/markets?order=volumeNum&ascending=false&closed=…`.
   - Raw market dicts; failure → `[]`. (Universe selection for the archiver
     now, for discovery scoring in Build 2.)

## Component 2 — `algorithms/insider_flow/` (the new Algorithm)

```
algorithms/insider_flow/
  __init__.py       # exports InsiderFlowAlgorithm, InsiderFlowParams
  params.py         # frozen dataclass, INSIDERFLOW_* env vars
  algorithm.py      # InsiderFlowAlgorithm(Algorithm)
```

### params.py — `InsiderFlowParams` (frozen dataclass, env-overridable)

| Param | Env | Default | Meaning |
|---|---|---|---|
| `name` | — | `insider_flow` | |
| `mode` | `INSIDERFLOW_MODE` | `paper` | fail-safe default |
| `poll_interval_seconds` | `INSIDERFLOW_POLL_INTERVAL` | `15` | firehose cadence |
| `min_cash_size_usdc` | `INSIDERFLOW_MIN_CASH` | `5000` | observed-trade notional floor |
| `max_entry_odds` | `INSIDERFLOW_MAX_ODDS` | `0.35` | only copy long-shot BUYs |
| `max_wallet_age_days` | `INSIDERFLOW_MAX_WALLET_AGE_DAYS` | `14` | freshness window |
| `max_prior_trades` | `INSIDERFLOW_MAX_PRIOR_TRADES` | `10` | freshness trade-count gate |
| `bet_size_usdc` | `INSIDERFLOW_BET_SIZE` | `10` | our copy size (top-up target) |
| `exclude_title_patterns` | `INSIDERFLOW_EXCLUDE_TITLES` | `" vs. ", " vs ", "O/U", "Spread"` | cheap sports filter (comma-sep env) |
| `settle_check_every` | `INSIDERFLOW_SETTLE_EVERY` | `20` | polls between resolution sweeps |
| `firehose_limit` | `INSIDERFLOW_FIREHOSE_LIMIT` | `100` | rows per poll |
| risk knobs (AlgoParams protocol) | `INSIDERFLOW_MAX_POSITION` etc. | pos `10` / expo `100` / loss `50` / min order `1` / slippage `0.10` | wider slippage than copy_trade — these signals move fast |
| `order_type`, `paper_starting_balance`, `paper_fee_bps` | std | `market` / `10000` / `0` | protocol compliance |

### algorithm.py — behavior contract

* `setup(tracker, notifier_mod, client)` — store tracker; `paper = mode==PAPER
  or client is None`; **seed the dedupe ring** with the current firehose page so
  a restart never replays history.
* `poll()`:
  1. `fetcher.fetch_global_trades(min_cash, firehose_limit)`, dedupe on
     `tx:wallet:asset:side` (one tx can carry multiple maker fills).
  2. Cheap local filters, in order: side==BUY → `0 < price ≤ max_entry_odds`
     → `cash_usdc ≥ min_cash_size` (defense in depth vs the API filter) →
     title not matching any exclude pattern → skip if our position already at
     `bet_size` (`tracker.get`).
  3. Only then the expensive check: `fetcher.fetch_wallet_stats(wallet)`.
     Fresh ⇔ `stats is not None and not capped and trade_count ≤
     max_prior_trades and oldest_ts ≥ now − max_wallet_age_days`. `None` →
     skip (fail closed).
  4. Yield `OpenIntent(market_id, asset_id, usdc_amount=bet_size − current_cost,
     signal_price=row.price, reason="fresh wallet 0xabcd…ef bet $52,581 @ 0.18")`.
  5. Every `settle_check_every` polls: for each `tracker.all_open(paper)`,
     `fetcher.fetch_market_resolution(market_id)`; resolved → `SettleIntent`.
     (v1 exit = hold to resolution. Mirroring the source wallet's SELL is a
     fast-follow once paper data shows whether insiders dump pre-resolution.)
* Bounded LRU dedupe ring identical to copy_trade (`SEEN_IDS_MAX`).
* `display_name` → `"insider_flow → fresh-wallet flow"` style self-description.

Everything downstream (risk caps, slippage gate, paper fills, DB writes,
Discord) is inherited via `runner.dispatch` — zero runner changes in this build.

## Component 3 — `discovery/archive.py` (price-history hoarder)

```
discovery/
  __init__.py
  archive.py        # library + CLI: python -m discovery.archive --once
```

* Own SQLite file (default `discovery_archive.db`, env `DISCOVERY_ARCHIVE_DB`);
  WAL; schema:
  - `price_history(token_id TEXT, fidelity INTEGER, ts INTEGER, price REAL,
     PRIMARY KEY(token_id, fidelity, ts))` — idempotent upserts.
  - `tracked_markets(condition_id TEXT PRIMARY KEY, token_ids TEXT,
     question TEXT, source TEXT, first_seen INTEGER, last_snapshot INTEGER)`
* Library functions (each independently testable, HTTP only via fetcher):
  - `connect(db_path)` — create schema if absent.
  - `upsert_history(conn, token_id, fidelity, points)` → rows-added count.
  - `track_market(conn, market_dict, source)` — register conditionId+tokens.
  - `collect_universe(conn, min_cash, top_n)` — union of: active top-volume
    markets (`fetch_top_markets(closed=False)`), recently-closed
    (`closed=True, end_date_min=now−7d`), whale-touched (`fetch_global_trades`).
  - `snapshot_all(conn, fidelity=60)` — `fetch_price_history` per tracked
    token; `None` (HTTP fail) → skip + keep going; updates `last_snapshot`.
  - `run_once()` / CLI `--once` (cron-friendly) and `--loop --every 3600`.

## Component 4 — wiring + docs

* `algorithms/profiles/experimental.py`: add a paper `InsiderFlowAlgorithm()`
  (env-default params). Existing profile test stays green (all-paper).
* `CLAUDE.md`: add `algorithms/insider_flow/` + `discovery/` to the tree,
  `INSIDERFLOW_*` env table, archiver run instructions.
* `.gitignore`: `discovery_archive.db*`.

## Test plan (written first)

`tests/test_fetcher.py` (append — module convention):
1. parse global trade: field mapping + `cash_usdc = shares × price`.
2. parse global trade: missing tx/conditionId/proxyWallet/asset → None; SELL kept.
3. `fetch_global_trades` passes `filterType=CASH`/`filterAmount`/`limit`; HTTP error → `[]`.
4. `fetch_wallet_stats`: counts TRADE rows, finds oldest ts, `capped=False` on short page.
5. `fetch_wallet_stats`: full page → `capped=True`; HTTP error → `None`.
6. `fetch_price_history`: parses `history`; empty body → `[]`; HTTP error → `None`.
7. `fetch_top_markets`: param construction; HTTP error → `[]`.

`tests/test_insider_flow.py` (new):
1. happy path: long-odds big BUY from fresh wallet → one `OpenIntent` with
   correct sizing/price/market/asset; reason mentions the wallet.
2. high-odds BUY skipped. 3. SELL row skipped. 4. sub-min cash skipped.
5. sports title pattern skipped. 6. wallet too old skipped.
7. too many prior trades / capped page skipped. 8. stats `None` → skipped (fail closed).
9. freshness lookup NOT called when cheap filters already rejected (API economy).
10. dedupe: same row across two polls → one intent.
11. top-up semantics: existing position at bet size → skip; partial → difference.
12. settlement sweep: resolved Gamma market + open position → `SettleIntent`
    (with `settle_check_every=1`); unresolved → nothing.
13. `setup()` seeds dedupe ring → first poll on same rows yields nothing.
14. params: `INSIDERFLOW_*` env overrides honored; default mode is PAPER.
15. display_name self-identifies.

`tests/test_archive.py` (new):
1. `connect` creates schema; `upsert_history` idempotent (re-insert → 0 added).
2. `snapshot_all` stores points for tracked tokens (fetch stubbed).
3. fetch failure (`None`) → no rows, no crash, other tokens still snapped.
4. `collect_universe` unions + dedupes the three sources; `track_market`
   upsert keeps `first_seen`.
5. CLI `run_once` smoke (everything stubbed) — returns counts.

Acceptance: full `pytest` suite green (existing 1.5k lines of tests must not
regress), no live HTTP in any test.
