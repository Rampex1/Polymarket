# Operations Manual — Deployment & Paper-Phase Monitoring

Post-merge runbook for getting the discovery work (InsiderFlow + price
archiver + signal logging) running on the VPS, and what to watch afterward.

VPS: `opc@148.116.94.154` — connect with `bash scripts/ssh_vm.sh`.

---

## 1. Local cleanup (after the PR merges)

```bash
git checkout main && git pull
git branch -d new_feature_uwu
```

## 2. Update the VPS

```bash
bash scripts/ssh_vm.sh
# then on the VPS:
cd ~/Polymarket                   # adjust if the repo lives elsewhere
git pull
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data && mv positions.db* data/ 2>/dev/null   # adopt new layout
```

Do the `mv` while the bot is **stopped** (restart window). Skipping the move
also works — the legacy fallback keeps `./positions.db` functional and just
logs a warning on every boot.

## 3. Create `.env.experimental` on the VPS (if missing)

```
DISCORD_WEBHOOK_URL=<webhook — consider a separate channel for paper noise>
TIMEZONE=America/Los_Angeles
```

**Deliberately NO `POLY_*` credentials.** That is the safety guarantee: the
experimental (paper) profile cannot touch real money even if misconfigured.
All `INSIDERFLOW_*` knobs have sane defaults; nothing else is required.

## 4. Seed the archive with local data (one-time, worth doing)

The laptop's archive holds price points for recently-resolved markets whose
history is now gone from the public API — irreplaceable. Ship it up before
the VPS archiver's first pass:

```bash
# from the laptop
scp -i ~/.ssh/ssh-key-2026-05-31.key data/discovery_archive.db \
    opc@148.116.94.154:~/Polymarket/data/
```

## 5. Start both processes (tmux survives disconnects)

```bash
tmux new -d -s paper   'cd ~/Polymarket && source .venv/bin/activate && PROFILE=experimental python main.py'
tmux new -d -s archive 'cd ~/Polymarket && source .venv/bin/activate && python -m discovery.archive --loop --every 3600'
```

Useful tmux: `tmux ls` (list), `tmux attach -t paper` (view), `Ctrl-b d`
(detach without killing), `tmux kill-session -t paper` (stop).

## 6. Verify within the first 10 minutes

- `tmux attach -t paper` →
  `[insider_flow_paper] Watching global flow ≥ $5000 at odds ≤ 0.35 (seeded N existing rows)`
  plus the two copy-trade paper workers starting.
- Discord shows three startup messages (one per algorithm).
- `tmux attach -t archive` → first-pass summary with `tracked_added` /
  `points_added` counts.
- NO `Using legacy ./positions.db` warning (if the step-2 move was done).

## 7. Watch cadence

| When | What |
|---|---|
| Day 1–2 | Glance at Discord. `SUSPICIOUS FLOW` lines in the paper log = detector firing. |
| Week 1–2 | Run the queries below: signal rate (a handful/day is healthy; dozens = filters too loose, zero/week = too tight), `skip_reason` distribution, feature null rates. |
| Monthly | Win rate vs. implied odds on resolved signals — the number the whole thesis rides on. |

### Observability queries (`sqlite3 data/positions.db`)

```sql
-- Signal rate per day
SELECT date(ts,'unixepoch') d, COUNT(*) FROM signals
WHERE algo='insider_flow_paper' GROUP BY d ORDER BY d;

-- Skip-reason distribution (many 'slippage' → signals move fast;
-- many 'risk:' → caps binding too early)
SELECT skip_reason, COUNT(*) FROM signals
WHERE executed=0 GROUP BY skip_reason;

-- Resolved outcomes: do ~0.20-odds bets win materially more than 20%?
SELECT signal_price, outcome, pnl_usdc FROM signals
WHERE outcome IS NOT NULL ORDER BY outcome_ts;

-- Feature health: /value reliability, wallet ages we're actually catching
SELECT json_extract(features,'$.portfolio_value_usdc') AS pv,
       json_extract(features,'$.wallet_age_seconds')/86400.0 AS age_days,
       json_extract(features,'$.detect_latency_seconds') AS latency_s
FROM signals WHERE algo='insider_flow_paper';
```

### Archive health (`sqlite3 data/discovery_archive.db`)

```sql
SELECT COUNT(*) FROM tracked_markets;
SELECT COUNT(*) FROM price_history;
SELECT datetime(MAX(last_snapshot),'unixepoch') FROM tracked_markets;  -- should be < 2h old
```

## 8. Prod restart (no urgency)

The merge changed code prod uses (`runner`, `fetcher`, `copy_trade`), but the
running prod process keeps the old code until restarted. Nothing in the merge
fixes a prod bug, so restart whenever convenient. Bonus on restart: the live
copy-trade algorithm starts logging signal rows too — free training data.

---

## What's next (code work, future sessions)

1. **Batch outcome labeler** — skipped signals never settle (no position was
   opened), so a periodic job should resolve `signals.unlabeled_market_ids()`
   against Gamma and label the counterfactuals.
2. **Build 2 — statistical account scorer** (`discovery/score.py` etc.):
   Poisson-binomial Z-test + Benjamini–Hochberg FDR over wallet histories,
   Closing-Line-Value per wallet from the archive → ranked `candidates.json`
   → auto-boot paper followers.
3. **Small-insider detectors**, in value order: holder-composition sweeps on
   thin news markets, on-chain funding provenance, firehose cluster detection.
4. **Confidence-weighted sizing** — once enough labeled rows exist, fit a
   simple model (logistic regression) on logged features and scale
   `bet_size_usdc` via `bot/sizing.stake`.

**Checkpoint:** review paper results ~60–90 days after deploy before
promoting anything to live.
