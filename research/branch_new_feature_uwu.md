# Branch: `new_feature_uwu`

Base: `4b79dc2` (Surface target wallet in Discord notifications)
7 commits, 201 tests passing.

✅ **Rebased onto `origin/main`** (2026-06-10): the branch now sits on top of
main's live-trading work (port to `py_clob_client_v2` for smart-wallet
accounts, FAK instead of FOK market orders, configurable
`POLY_SIGNATURE_TYPE`, funder diagnostics, smoke-test 1:1 mirror). The
rebase auto-merged with zero conflicts; full suite green (204 tests). The
import-isolation guard was extended to block `py_clob_client_v2` as well.
The branch is PR-ready against main.

---

## What has been done (this branch)

1. **Discovery primitives in `bot/fetcher.py`** — platform-wide `/trades`
   whale firehose (`GlobalTrade`), wallet freshness stats (fail-closed,
   null-timestamp safe), wallet portfolio value (`/value`), CLOB price
   history, Gamma top-markets. Shared `SeenRing` LRU dedupe and
   `config.env_value` helper. `market_is_resolved` moved out of runner so
   paper-only deploys don't need `py_clob_client`.

2. **InsiderFlow algorithm** (`algorithms/insider_flow/`) — copies the
   documented insider fingerprint: fresh wallet + large first bet + long odds
   + non-sports market. Cheap-to-expensive filter chain, one network call
   (wallet vetting, fail closed, 5-min verdict cache), hold-to-resolution
   exits. Paper mode in the experimental profile. All knobs `INSIDERFLOW_*`.

3. **Price-history archiver** (`discovery/archive.py`) — hoards CLOB price
   series before resolution deletes them. Active + recently-closed +
   whale-touched market universe, both outcome tokens per market, delta
   fetches after the first pass. `--once` (cron) or `--loop`.

4. **Signal feature logging** (`bot/signals.py` + `signals` table) — every
   dispatched `OpenIntent` leaves a training-data row: raw features at signal
   time (odds, cash, wallet age, portfolio value, detect latency, ...),
   execution outcome (filled / risk-blocked / slippage / no-fill), and
   close price + realized P&L backfilled automatically at settlement.
   Recording can never raise into the dispatch path.

5. **Kelly sizing math** (`bot/sizing.py`) — pure functions: `kelly_fraction`,
   `implied_belief` (inverts an observed bet into a belief lower bound),
   `edge`, fractional-Kelly `stake`. Deliberately NOT wired into any
   algorithm yet — flat $10 sizing keeps early outcome data unconfounded.

6. **`data/` directory** — both SQLite files default under `data/`
   (gitignored). Legacy fallback keeps a root-level `positions.db` working
   with a warning so a deploy that pulls this can't silently start fresh.

7. **Hygiene** — 39 new tests (201 total), code-review fixes (8 findings),
   CLAUDE.md in lockstep, plan docs in `research/`.

---

## What to observe (paper phase, after deploy)

Deploy = two processes on the VPS:
`PROFILE=experimental python main.py` and
`python -m discovery.archive --loop --every 3600`.

**Signal flow & quality**
- Signal rate: `SELECT COUNT(*) FROM signals WHERE algo='insider_flow_paper'`
  per day. Expect a handful; dozens/day means filters are too loose,
  zero/week means too tight (or the firehose filter drifted).
- `skip_reason` distribution: many `slippage` skips → these signals move
  fast, consider whether 0.10 is too tight; many `risk:` blocks → caps
  binding before the strategy can express itself.
- Win rate on resolved bets vs implied odds: the whole thesis is that
  ~0.20-odds copied bets win **materially more** than 20% of the time.
  `SELECT outcome, signal_price, pnl_usdc FROM signals WHERE outcome IS NOT NULL`.

**Feature health**
- `portfolio_value_usdc` non-null rate (is `/value` reliable?).
- `detect_latency_seconds` — how stale are signals when we see them? If
  routinely > poll interval, the firehose window/limit needs tuning.
- `wallet_age_seconds` distribution of copied wallets — are we actually
  catching day-old wallets, or two-week-old ones?

**Infrastructure**
- `data/positions.db` is created on first boot; the legacy-fallback warning
  should NOT appear on a fresh deploy.
- Archive growth per pass (logged summary): `tracked_added`, `points_added`,
  `failures`. Failures should be a small minority; sustained growth in
  tracked markets is expected.
- Paper bankroll & exposure in the Discord daily summary tagged
  `insider_flow_paper → fresh-wallet flow`.

---

## What's next

1. ~~Rebase onto current `origin/main`~~ — done 2026-06-10, zero conflicts,
   204 tests green.
2. **Deploy** both processes on the VPS. Every undeployed week is labeled
   training data and price history permanently lost.
3. **Batch outcome labeler** — skipped signals never settle (no position),
   so a periodic job should resolve `signals.unlabeled_market_ids()` against
   Gamma and label them. They're the counterfactuals ("signals we declined").
4. **Build 2 — statistical account scorer** (`discovery/score.py` etc.):
   Poisson-binomial Z-test + Benjamini-Hochberg FDR over wallet histories,
   Closing-Line-Value per wallet from the archive. Output: ranked
   `candidates.json` → auto-boot paper followers via a discovery profile.
5. **Small-insider detectors**, in value order: holder-composition sweeps
   on thin news markets (`/holders`), on-chain funding provenance
   (same-source fresh-wallet clusters), firehose time-window cluster
   detection.
6. **Confidence-weighted sizing** — once enough labeled signal rows exist,
   fit a simple model (logistic regression is enough) on the logged features
   and scale `bet_size_usdc` via `bot/sizing.stake`. Checkpoint: review
   paper results ~60–90 days after deploy before any live promotion.
