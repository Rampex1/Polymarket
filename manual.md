# Operations Manual — Deploy & Monitoring

Standing runbook for the VPS: how to deploy, what to check after, and the
queries that tell you whether the paper phase is working.

VPS: `opc@148.116.94.154` — connect with `bash scripts/ssh_vm.sh`.

---

## 1. Deploy — one command

```bash
bash scripts/ssh_vm.sh
# then on the VPS:
cd ~/Polymarket && bash scripts/setup_vm.sh
```

`setup_vm.sh` is the whole deploy: git pull, venv + deps, `data/` layout
migration, env hygiene (removes stale non-secret keys, verifies the five
`POLY_*` creds), profile validation, and a clean restart of the tmux
sessions with crash-visible panes. Idempotent — re-run it after every
push. It exits non-zero if a session dies at boot and prints that
session's last output.

Pushes to `main` run it automatically (`.github/workflows/ci.yml` →
`deploy` job, gated on the test job passing). Running it by hand is for
when you want to redeploy without a push, or to watch it happen.

> **Editing `setup_vm.sh` itself:** the script `git pull`s before it runs
> the rest of itself, so a version already executing is not the version
> that just landed. After changing the script, run it twice — once to
> pull, once to actually execute the new logic.

### Sessions it starts

| Session | Command | Notes |
|---|---|---|
| `paper` | `PROFILE=experimental python main.py` | always |
| `prod` | `PROFILE=prod python main.py` | **only if `config/prod.toml` has `[[algorithm]]` blocks** — see §5 |
| `archive` | `python -m discovery.archive --loop --every 3600` | always |
| `discord` | `python scripts/run_discord_bot.py` | always; all profiles, one token |

An intentionally-empty `prod.toml` is not an error: the script detects it,
kills any stale `prod` session, prints `no [[algorithm]] blocks configured`,
and continues. It only verifies the sessions it actually started.

## 2. Env files on the VPS

One `.env`, holding **only** the five `POLY_*` creds, plus
`DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` for the slash-command bot.
Webhooks route per-profile via the committed `config/webhooks.toml`.

`setup_vm.sh` enforces this: it deletes `.env.experimental` (a leftover
one shadows `.env` and its stale webhook overrides the registry) and
strips `DISCORD_WEBHOOK_URL`, `TARGET_ADDRESS`, `COPYTRADE_TARGET_ADDRESS`,
`TIMEZONE`, and a redundant `POLY_SIGNATURE_TYPE=3` if it finds them.

The paper-can't-touch-money guarantee lives in config, not in env-file
separation: the loader rejects any `mode = "live"` block in a profile
without top-level `allow_live = true`, and only `prod.toml` sets it.
Sanity-check what will run with:

```bash
PROFILE=experimental python -m bot.params --effective
PROFILE=prod        python -m bot.params --effective
```

## 3. tmux cheat-sheet

```bash
tmux ls                        # what's running
tmux attach -t paper           # view a session
# Ctrl-b d                     # detach without killing
tmux kill-session -t paper     # stop one
```

Panes survive a process crash (`remain-on-exit`), so attaching after a
death shows the traceback rather than an empty screen.

## 4. Verify after a deploy

- `tmux attach -t paper` → the insider-flow worker announcing its filters,
  e.g. `[insider_flow_paper] Watching global flow ≥ $5000 at odds ≤ 0.35
  (seeded N existing rows)`. One startup line per `[[algorithm]]` block in
  `config/experimental.toml`.
- Discord: one startup message per algorithm, in the channel
  `config/webhooks.toml` maps that profile to.
- `tmux attach -t archive` → first-pass summary with `tracked_added` /
  `points_added` counts.
- `tmux attach -t discord` → bot connected, slash commands synced. Try
  `/status`.
- NO `Using legacy ./positions.db` warning — the DB should live at
  `data/positions.db`.

## 5. Resuming prod

`config/prod.toml` currently declares `allow_live = true` and **zero**
algorithm blocks — prod is paused, no live trading, and the `prod` tmux
session is intentionally absent. To resume: copy a proven block from
`experimental.toml`, flip `mode = "live"`, keep the `name` stable (it's
the DB partition key — renaming orphans that algorithm's bankroll and
history), commit, and run `setup_vm.sh`.

Note that a running worker keeps its old code until restarted, so a
deploy that changes `runner` / `fetcher` / an algorithm only takes effect
on the next `setup_vm.sh`.

## 6. Watch cadence

| When | What |
|---|---|
| Day 1–2 | Glance at Discord. `SUSPICIOUS FLOW` lines in the paper log = detector firing. |
| Week 1–2 | Signal rate (a handful/day is healthy; dozens = filters too loose, zero/week = too tight), `skip_reason` distribution, feature null rates. |
| Monthly | Win rate vs. implied odds on resolved signals — the number the whole thesis rides on. |

**Checkpoint:** review paper results ~60–90 days after deploy before
promoting anything to live.

### Observability queries (`sqlite3 data/positions.db`)

Most of this is canned in `python -m bot.report` (per-algo P&L, signal
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

## 7. Merging archive data between machines

Never `scp` one `discovery_archive.db` over another — both machines
accumulate history the other lacks, and an overwrite destroys data.
Upload under a temp name and merge (both tables have natural PKs, so
`INSERT OR IGNORE` dedupes exactly):

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
