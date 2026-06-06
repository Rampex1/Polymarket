# TODO — Outstanding work

Tracks every finding from the senior review that wasn't closed in the
`design-improv` branch. Items are grouped by the **original review severity**
so it's clear what risk you're still carrying. Inside each section,
finer-grained ranking is **P0 → P3**.

At the bottom you'll find:
  * **Strategic open questions** — design decisions, not unit tasks.
  * **What got fixed** — a checklist of what shipped on this branch, so you
    can verify nothing was silently dropped.

---

## 🔴 CRITICAL — Correctness / money-at-stake (remaining)

All eight original CRITICAL items from the review shipped — see "What got
fixed" below. The items here surfaced **during** the fixes and need
follow-up before going live.

### P0 — Verify `_parse_fill` against real CLOB responses
- **Why:** The new `_parse_fill` assumes specific field names
  (`takingAmount`, `makingAmount`, `success`, `status`). If the live API
  uses different names or omits them on partial fills, we'll silently
  mis-record fills — same class of bug we just fixed, different layer.
- **Where:** `bot/executor.py:_parse_fill`. Capture a real response with
  a $1 test order and pin the shape in `tests/test_executor.py`.
- **Cost:** Low — but needs one real live trade to capture.

### P0 — Reconciliation between bot DB and exchange truth
- **Why:** If we ever drift (mid-order crash, malformed response, manual
  trade on the same wallet), there's no self-healing — the DB silently
  diverges from reality forever.
- **Where:** New `bot/reconcile.py` that fetches our own wallet's positions
  on startup (and every N minutes) and corrects any deltas.
- **Cost:** Medium.

### P1 — Idempotent order placement / pending-orders ledger
- **Why:** If we send an order and crash before recording it, we don't
  know on restart whether the order went through. Write-ahead pending row,
  marked complete after the fill is recorded, fixes this.
- **Where:** `bot/db.py` (new table), `bot/executor.py` (write-ahead).
- **Cost:** Medium.

---

## 🟠 HIGH — Trading algorithm / financial logic (remaining)

These are the original HIGH-severity items from the review that were
**not** closed on this branch — mostly because they're strategy decisions,
not bug fixes.

### P0 — Replace polling with the Polymarket websocket feed
*(Original finding #10 — "No latency budget — you're racing a 20–60s old signal.")*
- **Why:** Latency is the single biggest edge-killer in copy trading.
  20s polling + Data-API lag + own RTT = signals arrive 25–60s stale.
  By then the price has often already moved past the slippage threshold.
- **Where:** `bot/fetcher.py` — replace `poll()` with a websocket subscriber
  feeding the same `on_trade` callback. Keep polling as a disconnect fallback.
- **Cost:** Medium.

### P0 — Conviction sizing based on signal *delta*, not cumulative holding
*(Original finding #9 — "Tier sizing uses cumulative holding, not signal strength.")*
- **Why:** A target who builds a $300k position over ten $30k buys hits
  Tier 3 ($3) on every subsequent $1k buy regardless of how strong each
  signal actually is. The real conviction signal is the *size of the new
  trade* relative to that trader's typical bet.
- **Where:** `bot/executor.py:_tier_for_holding` → replace or stack with
  `scale = trade.size_usdc / rolling_median(target_trade_size)`.
- **Cost:** Low code; needs a small rolling-stats pipeline.

### P1 — Independent exit logic (stop-loss, take-profit, time-decay)
*(Original finding #12 — "Bot mirrors losses indefinitely with no independent exit.")*
- **Why:** The bot has no exit mechanism independent of the target.
  If the target rides a position to zero, so do we. At minimum: a hard
  drawdown floor ("exit at -50% and target hasn't traded in 7d").
- **Where:** Background sweep in `main.py` + helper in `bot/executor.py`.
- **Cost:** Medium-high — needs a strategy design call before coding.

### P1 — Conviction filtering: liquidity, spread, time-to-resolution
*(Original finding #15 — "No conviction-based filtering.")*
- **Why:** Today every signal above tier-1 threshold gets copied. There's
  no filter for thin books, wide spreads, or markets resolving in 24h vs.
  6 months — all of which materially change risk.
- **Where:** New gate in `bot/executor.py:_handle_buy` between tier
  decision and risk check. Needs market-detail fetch from Gamma.
- **Cost:** Medium.

### P1 — Historical hit-rate tracking per tier
*(Original finding #40 — "No correlation tracking … naive copy can be -EV.")*
- **Why:** Without knowing the target's tier-by-tier hit rate, we can't
  tell if Tier 3 ($3 bets on $300k+ positions) is the most or *least*
  profitable cohort. The bet sizing is currently uncalibrated.
- **Where:** New `scripts/target_stats.py` that walks `trade_log` and
  recent on-chain history; expose results to inform tier sizes.
- **Cost:** Low-medium.

### P2 — Tier thresholds as percentiles, not absolute USD
*(Original finding #41 — "Tier thresholds are absolute USD; relative would be better.")*
- **Why:** A target who scales their typical bet up over time pushes the
  whole strategy out of tier 1. Percentile-based thresholds (top-25%) self-
  adjust to their bet distribution.
- **Where:** Same place as the hit-rate tracker (#above) — share the stats.
- **Cost:** Low after #above lands.

---

## 🟡 MEDIUM — Code quality / efficiency (remaining)

### P1 — Cursor-paginated activity fetching
*(Original finding #23 — "Polling fetches full 100-trade list every cycle.")*
- **Why:** Always fetching the same 100 trades and diffing IDs is wasteful,
  and creates the "burst > 100 trades in 20s = silent miss" risk.
- **Where:** `bot/fetcher.py:fetch_recent_trades` — track high-watermark
  timestamp, only fetch newer. If Polymarket's API supports a cursor, use it.
- **Cost:** Low.

### P1 — Dedupe key beyond `transactionHash`
*(Original finding #30 — "Same tx can contain multiple fills.")*
- **Why:** `seen_ids` keys on `transactionHash`. A single on-chain tx can
  contain multiple fills — we'd see only one.
- **Where:** `bot/fetcher.py:_parse_trade` (already tightened to require
  asset_id) — switch dedupe to `(transactionHash, asset_id, side)` or
  include log index.
- **Cost:** Low.

### P2 — Healthcheck endpoint + Docker `HEALTHCHECK`
*(Original finding #31, #44 — "No metrics endpoint; no liveness probe.")*
- **Where:** Small HTTP server in `main.py` exposing `/health` with
  last-poll-success timestamp; add to `docker-compose.yml`.
- **Cost:** Low.

### P2 — Structured logging
*(Original finding #30 / #45 — "No structured logging.")*
- **Where:** Replace `logging.basicConfig` in `main.py` with JSON via
  `python-json-logger`.
- **Cost:** Low.

### P2 — Poll-failure rate alert
*(Original finding #46 — "No alert on poll failure rate.")*
- **Where:** `bot/fetcher.py:poll` — track consecutive failures, fire a
  Telegram alert past N misses.
- **Cost:** Low.

### P2 — Confirm fee model against real Polymarket fee schedule
- **Why:** `PAPER_FEE_BPS` is now configurable but defaults to 0; if real
  fees use a maker/taker split the paper accounting still drifts.
- **Where:** `bot/config.py` + `bot/executor.py:_simulate_*` — split into
  maker/taker bps if confirmed.
- **Cost:** Low (one doc lookup + minor code).

---

## 🟢 LOW — Polish / nice-to-have (remaining)

### P2 — DB backups + rotation
*(Original finding #48 — "No backups for positions.db.")*
- **Where:** `scripts/backup_db.sh` + cron via Docker.
- **Cost:** Low.

### P2 — Secrets handling beyond `.env`
*(Original finding #50 — "POLY_PRIVATE_KEY in plain .env.")*
- **Where:** Add support for AWS/GCP Secrets Manager or `pass`. Filter
  the key out of any log lines defensively.
- **Cost:** Medium.

### P2 — Process-level singleton lock
*(Original finding #51 — "No process-level lock; two instances could
double-copy.")*
- **Where:** `main.py` — `flock` on the DB file or a `.lock` sidecar.
- **Cost:** Trivial.

### P3 — Mark-to-market position valuation
*(Original finding #6 follow-up — `current_value_usdc` was renamed to
`cost_basis_usdc` precisely because it never marked to market; we still
have no MTM view anywhere.)*
- **Where:** New `Position.mark_to_market(mid_price)` and a batched
  price-fetch helper. Show in `print_summary` and the daily summary.
- **Cost:** Low-medium.

### P3 — Type-check + lint in CI
- **Where:** `pyproject.toml` with `ruff` + `mypy`; new CI workflow.
- **Cost:** Low.

### P3 — Backtest harness against historical activity
- **Why:** Today we can only evaluate strategy changes by waiting for live
  signals. A replay harness over the last N days of the target's activity
  cuts that to seconds.
- **Where:** New `scripts/backtest.py`.
- **Cost:** Medium.

### P3 — End-to-end replay test
- **Where:** Recorded `/activity` fixture → run through `poll()` → assert
  final DB state.
- **Cost:** Low (after #above lands).

### P3 — Wallet-lookup HTTP path test coverage
- **Where:** `tests/test_fetcher.py` — stub Gamma response.
- **Cost:** Trivial.

### P3 — Pause/resume mechanism
- **Why:** Today the only way to stop the bot is to kill the process. A
  flag (file or env-var hot-reload) to temporarily suspend new BUYs (but
  keep SELLs/REDEEMs flowing) would be useful during major news events.
- **Where:** Check a `.paused` sidecar file or Telegram command in
  `bot/executor.py:_handle_buy`.
- **Cost:** Low.

### P3 — Telegram bot commands
- **Why:** Right now Telegram is one-way (bot → us). Two-way (`/pause`,
  `/status`, `/close <market>`) would close the operability gap.
- **Where:** New `bot/telegram_commands.py` polling `getUpdates`.
- **Cost:** Medium.

---

## Strategic open questions

These are not "TODOs" — they're decisions you should make *before*
committing more engineering effort.

### Sizing makes no money at current limits
Tier bets of $1–$3 with a $10k bankroll can't escape fees + spread.
Either size up materially (and rebuild risk limits) or formally reframe
the bot as a research tool. Engineering more features is wasted effort
until this is resolved.

### Why this target?
`surfandturf` is hardcoded as the default. Capture the rationale in
`CLAUDE.md` — sample size, hit rate, blowup risk — so we can re-evaluate
without re-deriving the original thesis.

### Targets ≠ alpha
Following one trader presumes they have edge. Worth confirming with the
hit-rate tracker (HIGH #4) before scaling bets up.

---

## ✅ What got fixed on `design-improv`

For audit — the 50+ findings from the original review and their current state.

### Critical (all 8 closed)
- [x] #1 Fill price faked — executor reads actual fill from order response
- [x] #2 Orders recorded on failed fills — `FillResult` + only-on-success record
- [x] #3 Slippage skip in paper mode — now runs in paper too
- [x] #4 Post-trade race in target-holding fetch — `expected_min` retry
- [x] #5 Daily-loss limit blocks SELLs — SELLs/REDEEMs now bypass all checks
- [x] #6 `current_value_usdc` ≠ market value — renamed `cost_basis_usdc`
- [x] #7 SELL always full-close — now proportional to target's sell ratio
- [x] #8 `record_buy` silent abort on price≤0 — refuses + logs error

### High (4 of 8 closed; remainder above)
- [ ] #9 Tier sizing uses cumulative not conviction → above
- [ ] #10 Latency / websocket → above
- [x] #11 Fees in paper P&L — `PAPER_FEE_BPS` modeled in simulate + record
- [ ] #12 No independent exit logic → above
- [x] #13 REDEEM stuck-open on ambiguous mid — Gamma fallback resolves it
- [x] #14 `MAX_POSITION_SIZE` defaults disagree — reconciled
- [ ] #15 Conviction filtering → above
- [x] #16 `seen_ids` unbounded — LRU bounded at 5000

### Medium (8 of 11 closed; remainder above)
- [x] #17 `MIN_ORDER_SIZE_USDC` dead — now enforced in risk gate
- [x] #18 `flask` dead dependency — removed
- [x] #19 SQLite indexes — added on hot paths
- [x] #20 No WAL + concurrent threads — WAL + `synchronous=NORMAL` set
- [x] #21 `paper` flag inconsistency — single source of truth from main
- [x] #22 HTTP retries — `Retry` adapter on Session
- [ ] #23 Cursor-paginated polling → above
- [x] #24 Telegram HTML escape — all user strings escaped
- [x] #25 Daily summary local time — uses `config.TIMEZONE`
- [x] #26 Graceful shutdown — Event-driven, no mid-order interrupts
- [ ] #27 Order placement idempotency → above

### Low (most closed)
- [x] #28 Duplicate `today_pnl` in summary script — removed
- [x] #29 Reset-script docstring stale — fixed
- [ ] #30 Structured logging → above
- [ ] #31 Healthcheck/metrics → above
- [x] #32 No tests — added `tests/` (66 tests, 77% coverage on `bot/`)
- [x] #33 `Position.current_value_usdc` unused — renamed & used
- [x] #34 `trade` arg untyped in notifier — typed as `Trade`
- [x] #35 `TARGET_USERNAME` hardcoded — moved to env
- [x] #36 `.env.example` tier comment stale — rewritten

### Original review findings not in the numbered list above
- [ ] Reconciliation vs exchange (was #26 in original critical list) → above
- [ ] Dedupe duplicate fills inside one tx (#30 original) → above
- [ ] Healthcheck endpoint (#44 original) → above
- [ ] DB backups (#48 original) → above
- [ ] Secret management (#50 original) → above
- [ ] Singleton lock (#51 original) → above
- [ ] Poll-failure alert (#46 original) → above
- [ ] Pause mechanism (#43 original) → above
