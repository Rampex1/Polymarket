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

## 2. Update the VPS — one command

```bash
bash scripts/ssh_vm.sh
# then on the VPS:
cd ~/Polymarket && bash scripts/setup_vm.sh
```

`setup_vm.sh` does everything sections 2–5 used to describe by hand:
git pull, venv + deps, data/ layout migration, env hygiene (removes the
obsolete `.env.experimental`, verifies the five `POLY_*` secrets),
profile validation, and a clean restart of all three tmux sessions
(`paper`, `prod`, `archive`) with crash-visible panes. Idempotent —
re-run it after every push. It exits non-zero if any session dies at
boot and prints that session's last output.

## 3. Env files on the VPS

One `.env` holding only the five `POLY_*` creds — that's it. Webhooks
route per-profile via the committed `config/webhooks.toml` (arrives with
the git pull), and `.env.experimental` should NOT exist (a leftover one
shadows `.env` and its stale webhook overrides the registry — delete it).

The paper-can't-touch-money guarantee is enforced in config now, not by
env-file separation: the loader rejects any `mode="live"` block in a
profile without top-level `allow_live = true`, and only `prod.toml` sets
that. Sanity-check what will run with:

```bash
PROFILE=experimental python -m bot.params --effective
```

## 4. Merging archive data between machines (done 2026-06-12; keep for reference)

Never `scp` one `discovery_archive.db` over another — both machines accumulate
history the other lacks, and an overwrite destroys data. Upload under a temp
name and merge (both tables have natural PKs, so `INSERT OR IGNORE` dedupes
exactly):

```bash
scp -i ~/.ssh/ssh-key-2026-05-31.key data/discovery_archive.db \
    opc@148.116.94.154:~/Polymarket/data/laptop_archive.db
# then on the VPS:
cd ~/Polymarket/data && cp discovery_archive.db discovery_archive.db.bak && \
../.venv/bin/python - <<'EOF'
import sqlite3
db = sqlite3.connect("discovery_archive.db")
db.execute("ATTACH 'laptop_archive.db' AS laptop")
db.execute("INSERT OR IGNORE INTO price_history SELECT * FROM laptop.price_history")
db.execute("INSERT OR IGNORE INTO tracked_markets SELECT * FROM laptop.tracked_markets")
db.commit()
EOF
rm laptop_archive.db
```

## 5. tmux cheat-sheet (sessions are started by setup_vm.sh)

`tmux ls` (list), `tmux attach -t paper` (view), `Ctrl-b d` (detach without
killing), `tmux kill-session -t paper` (stop one). Panes survive a process
crash (`remain-on-exit`), so attach shows the traceback.

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

Most of these are canned in `python -m bot.report` (per-algo P&L, signal
counts, skip reasons, win rate vs. entry odds) — run that first; drop to
raw SQL for anything deeper.

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
