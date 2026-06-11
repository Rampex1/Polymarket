# Discovery Plan — Finding & Copying Suspicious Accounts (verified June 9, 2026)

Goal: replace the single hand-picked whale with a continuously-refreshed stable of
statistically-validated targets (high-PnL sharps + insider-pattern wallets), copied
for +EV. This supersedes nothing in `account_discovery_convo.txt` — it builds on it,
corrects it where live probes disagreed, and adds one major missing piece.

---

## 1. What I verified today (live probes, unauthenticated)

| Endpoint | Status | Notes |
|---|---|---|
| `data-api /trades?filterType=CASH&filterAmount=N` | ✅ | **Whale-trade firehose.** Platform-wide, every row has `proxyWallet`, side, price, size, conditionId, tx hash. The single best discovery primitive. |
| `data-api /trades?user=<addr>` | ✅ | Per-wallet trade feed. |
| `data-api /holders?market=<conditionId>` | ✅ | Works **even on long-resolved markets** (2025 NBA Finals still returns holders). Good for enumerating past winners. |
| `data-api /activity?user=&limit=500&offset=` | ✅ | Deep pagination works → full wallet history is reconstructable. |
| `data-api /value?user=` | ✅ | Wallet net worth. |
| `gamma /markets?closed=true&order=volumeNum&ascending=false` | ✅ | Universe of resolved markets. (`order=volume` is broken — use `volumeNum`.) |
| `clob /prices-history` | ⚠️ | Works at ~minute fidelity **but only for active/recent markets**. A 2025-resolved market returns **0 points** at every fidelity/interval, including explicit `startTs/endTs`. |
| Any leaderboard endpoint | ❌ | All 404 (lb-api, data-api, gamma, polymarket.com/api). Confirmed again today. |

**The `/prices-history` finding is the big constraint the convo notes missed:**
price history evaporates for old markets. Consequences:
- The lead-lag "insider timing fingerprint" only works on markets resolved recently
  (~the last month) or still active.
- A deep historical copy-execution backtest (Backtest 2 in the notes) is **not
  possible** with public data. Forward paper-trading is the real validation, and we
  should start **archiving price history ourselves** now so future backtests exist.

## 2. What the public record says insiders look like (changes the design)

Documented 2025–26 cases: Army Master Sergeant betting ~$34k before the Venezuela
operation (charged); Google engineer making $1.2M on "most-searched person" with
inside knowledge (charged); six accounts netting $1.2M hours before the Iran
strikes; a brand-new wallet betting $40k on OpenAI launching a browser weeks early.

Common fingerprint: **fresh wallet, no history, large first bets, long odds,
news-driven market.** A whole cottage industry (PolyInsider, PolyTrack, PolyWatch,
FirePolymarket) keys on exactly "new wallet + $5k+ first-time bet."

**Design consequence:** the Z-test pipeline requires N≥30 resolved bets — it can
*never* catch a fresh-wallet insider. We need two complementary detectors:

- **Detector A — longitudinal sharps** (the convo's Stage 1–2): wallets with long
  histories that beat market odds with statistical significance. Found offline,
  validated, then copied via the existing `CopyTradeAlgorithm`.
- **Detector B — live suspicious-flow** (new): fresh wallets making big long-odds
  bets *right now*, copied at detection time. No history needed. This catches the
  Venezuela/Iran archetype, which is where the documented insider money actually was.

---

## 3. What to build — by component

### Phase 1: `discovery/` package (offline, peer of `bot/`; Detector A)

```
discovery/
  __init__.py
  universe.py     # Stage 1 — enumerate candidate wallets
  history.py      # Stage 2a — pull + cache full /activity per wallet
  score.py        # Stage 2b — lucky-vs-skilled statistics
  timing.py       # Stage 2c — lead-lag fingerprint (recent markets only)
  archive.py      # price-history snapshotter (see Phase 4)
  report.py       # emits discovery/candidates.json
  run.py          # CLI: python -m discovery.run
  cache.sqlite    # gitignored; aggressive caching, it's all I/O bound
```

- **universe.py** — two feeds, union + dedupe:
  1. Poll `/trades?filterType=CASH&filterAmount=10000` (cron, every few min) →
     collect `proxyWallet`s. This is prospective: it builds the watch universe
     going forward.
  2. Gamma resolved markets by `volumeNum` (optionally by tag: politics /
     geopolitics / crypto / tech) → `/holders` per market → wallets. This is
     retrospective: past winners at size.
- **history.py** — full `/activity` pagination per wallet (500/page), normalized
  into the cache DB. Join each TRADE row to Gamma resolution via the existing
  `bot.fetcher.fetch_market_resolution`. Mark expired-worthless as losses.
- **score.py** — per wallet, exactly the convo's math (it's correct):
  - hard gates first: N_resolved ≥ 30; mark-to-resolution PnL > 0; median position
    size recorded (for tier calibration); recency (active in last 60d).
  - Z = (Σw_i − Σp_i) / sqrt(Σ p_i(1−p_i)) at entry odds p_i, one-sided p-value
    (Monte Carlo Poisson-binomial for small N).
  - Benjamini–Hochberg FDR ≈ 10% across all tested wallets.
  - category entropy over Gamma tags (low entropy = real edge), recency-weighted
    re-score (last 6 months).
- **timing.py** — shortlist only, recent markets only (data constraint): share of
  entries that preceded a ≥10pt favorable move within 24–48h, from
  `/prices-history`. Flags "insider-pattern" vs merely "sharp."
- **report.py** → `discovery/candidates.json`:
  ```json
  [{"wallet":"0x…","z":3.8,"q":0.02,"n":64,"median_size":120000,
    "suggested_tier1_min":60000,"suggested_tier1_max":110000,
    "timing_flag":true,"categories":["geopolitics"]}]
  ```
  `suggested_tier*` derived from the wallet's own size distribution (e.g. tier1_min
  = 0.5 × median position) — **never reuse the $80k default blindly**; a $5k-median
  sharp would otherwise never trigger a single copy.

### Phase 2: `algorithms/insider_flow/` (Detector B — new Algorithm, highest EV/effort)

This is the piece missing from the convo notes, and it slots perfectly into the
existing architecture (subclass `Algorithm`, yield Intents, inherit runner/risk/
Discord for free):

- `poll()` hits `/trades?filterType=CASH&filterAmount=<min_cash>` (platform-wide,
  not per-wallet), dedupes by tx hash, then filters each BUY for:
  - price ≤ `max_entry_odds` (default ~0.35 — long odds is where insider EV lives;
    also auto-excludes sports market-makers buying at 0.99),
  - wallet freshness: first `/activity` page → account age ≤ `max_wallet_age_days`
    (default 14) or prior trade count ≤ `max_prior_trades` (default ~10),
  - optional category allowlist (geopolitics/politics/tech/crypto; sports off by
    default — big fresh sports bets are usually just gamblers),
  - optional: total wallet concentration (bet ≥ X% of `/value`) — conviction proxy.
- Emits `OpenIntent` sized by *bet size* tiers (not holding tiers — fresh wallets
  have no $80k holdings). Exit: hold to resolution (`SettleIntent` on REDEEM
  detection via the wallet's activity) plus mirror their SELL if it appears.
- Params dataclass (`INSIDERFLOW_*` env): `min_cash_size`, `max_entry_odds`,
  `max_wallet_age_days`, `max_prior_trades`, `bet_size`, `max_positions`,
  category list, poll interval (10–15s is fine; $10k+ trades aren't frequent).
- **Paper mode only** until it has weeks of track record.

### Phase 3: `algorithms/profiles/discovery_paper.py` (validation loop)

- Reads `discovery/candidates.json` → boots one `Mode.PAPER` `CopyTradeAlgorithm`
  per candidate (cap ~10–15 workers; stagger `poll_interval_seconds` to be polite
  to the API) with the per-candidate calibrated tiers. Plus one paper
  `InsiderFlowAlgorithm`.
- Promotion rule (write it down, follow it mechanically): after ≥3 weeks AND ≥10
  copied trades, rank by paper PnL net of skips; promote top 1–2 into `prod.py` as
  `Mode.LIVE` with conservative sizing; kill the rest. Re-run discovery monthly.

### Phase 4: `discovery/archive.py` — start hoarding price history NOW

Because CLOB history evaporates at resolution: a small cron/loop that snapshots
`/prices-history` (hourly fidelity, plus minute fidelity for watchlist-touched
markets) into the cache DB for every market any watched/candidate wallet touches.
Cheap insurance; in 2–3 months you own the backtest dataset that doesn't exist
publicly, including everything needed for honest copy-execution replays
(detection-lag fills, slippage-gate pass/fail).

### Phase 5: Backtester (scoped to what data permits)

- **Backtest 1 (does the wallet have edge?)** — fully feasible historically from
  `/activity` + Gamma resolutions. This *is* `score.py`; no separate tool needed.
- **Backtest 2 (would WE profit copying them?)** — only feasible over the recent
  window + our growing archive. Implement as replay: detect at t+15s, fill from
  archived price path, apply the real tier/slippage/min-order logic from
  `runner.py`. Until the archive matures, **paper-forward is the authoritative
  test** — and it's free in this architecture.

### Phase 6: Thin agent layer (shortlist only)

For the top ~10 candidates: web-search whether their best-timed bets preceded
public reporting (separates "insider" from "fast"); sanity-check resolution
criteria on markets that dominate their PnL. Qualitative gate before promotion,
not part of scoring.

---

## 4. Changes to the EXISTING codebase (small but load-bearing)

1. **Slippage gate inversion** (`runner.py:388`, `params.py max_slippage=0.05`):
   on insider-grade signals the price often moves >5% within the detection window,
   so the gate systematically keeps the priced-in trades and drops the alpha.
   Change: add `slippage_mode` to params — `"skip"` (current) vs `"chase"` (cap
   entry at `signal_price + max_chase_cents`, e.g. buy up to 0.45 on a 0.30
   signal, via limit order). InsiderFlow paper instances should run `"chase"`;
   measure the difference in paper before any live change.
2. **Per-candidate tier calibration** — already supported (per-instance params);
   the discipline is that `discovery_paper.py` must *always* set tiers from
   `candidates.json`, never inherit the $80k defaults.
3. **`bot/fetcher.py` additions** (pure additions, no behavior change):
   `fetch_global_trades(min_cash, limit)` (the firehose), `fetch_wallet_age(addr)`
   (first-activity timestamp + trade count via ascending `/activity`),
   `fetch_price_history(token_id, fidelity, start_ts, end_ts)`.
4. **Worker scale check** (`main.py`): 15+ paper workers is 15 threads × 1 poll
   per 20–30s — fine, but stagger startup and respect a global rate budget
   (shared token-bucket in `fetcher.SESSION`) so discovery + workers don't trip
   429s. The retry adapter already handles transient 429s.
5. **DB**: nothing to change — `algo` partitioning already isolates each paper
   follower. Discovery uses its own cache DB, never `positions.db`.

## 5. Build order (EV per unit effort, descending)

1. **InsiderFlow paper algorithm** (Phase 2) + fetcher additions — days of work,
   catches the documented-insider archetype immediately, zero risk in paper.
2. **Price-history archiver** (Phase 4) — ~an afternoon; every week of delay is a
   week of backtest data lost forever.
3. **discovery/ scoring pipeline** (Phase 1) — the statistical engine for sharps.
4. **discovery_paper profile + promotion discipline** (Phase 3).
5. **Slippage chase-mode A/B** in paper (Phase 4 change list, item 1).
6. **Copy-execution backtester** (Phase 5) once the archive has ≥1 month of data.
7. Agent layer (Phase 6) when there's a real shortlist to interrogate.

## 6. Honest risk notes (EV-relevant, not moralizing)

- **False positives in Detector B**: most fresh-wallet whales are rich gamblers,
  not insiders. The odds filter + category filter + paper validation are the
  defense; expect a low hit rate with fat right-tail payoffs at long odds.
- **Disputed resolutions**: markets where insiders trade attract UMA disputes and
  occasional voids; mark-to-resolution PnL should treat voided markets as
  refunds, not wins.
- **Copy capacity**: at $1–3 bets none of this matters financially. The point of
  paper validation is to justify raising tier sizes 10–100× on promoted targets;
  the risk caps (`max_position`, `max_exposure`, `daily_loss_limit`) scale with it.
- **Counterparty awareness**: copied wallets can notice followers (public books)
  and could in principle paint activity; per-target exposure caps bound this.
- Following insider trades is itself legal gray territory that's getting active
  DOJ/CFTC attention on the *insiders'* side; the copier's exposure is different
  but this space is moving — worth a periodic check.
