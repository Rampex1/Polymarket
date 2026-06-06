# CLAUDE.md — Polymarket Copy-Trading Bot

## Project Overview

A Python bot that automatically mirrors trades from a target Polymarket wallet. It detects BUY/SELL/REDEEM signals via API polling, applies tiered bet-sizing, enforces risk limits, and can run in paper (simulated) or live mode.

## Running the Bot

```bash
source .venv/bin/activate
python main.py
```

**Docker:**
```bash
docker compose up -d
docker compose logs -f
```

**Utility scripts:**
```bash
python scripts/reset_paper_trade_db.py      # Wipe and reset paper trading DB
python scripts/trading_account_summary.py   # Print portfolio snapshot
```

## Architecture

```
main.py                   # Entry point — wires all modules together, runs poll loop
bot/
  config.py               # All settings loaded from .env, module-level constants
  models.py               # Trade dataclass
  db.py                   # SQLite connection + schema (positions, trade_log, daily_stats, paper_account)
  fetcher.py              # Poll Data API for target wallet's trades, parse into Trade objects
  executor.py             # Dispatch BUY/SELL/REDEEM, apply tier sizing, slippage check, place CLOB orders
  positions.py            # PositionTracker (DB CRUD) + RiskManager (enforce limits)
  notifier.py             # Telegram alerts + midnight daily summary thread
scripts/
  reset_paper_trade_db.py
  trading_account_summary.py
  ssh_vm.sh
```

**Trade lifecycle:**
1. `fetcher.poll()` detects new trades by the target wallet
2. `executor.execute()` dispatches by action (BUY / SELL / REDEEM)
3. For BUY: fetch target's current holding → determine tier → risk checks → place order → record position
4. For SELL/REDEEM: close position → calculate P&L → record
5. `notifier` sends Telegram messages at each step

## Key Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `TARGET_ADDRESS` | — | Wallet to copy; required |
| `PAPER_TRADE` | `true` | Set `false` for live trading |
| `PAPER_STARTING_BALANCE` | `10000.0` | Virtual balance in USD |
| `TIER1_MIN` / `TIER1_MAX` / `TIER1_SIZE` | `80000` / `150000` / `1.0` | Tier 1: $1 bet |
| `TIER2_MAX` / `TIER2_SIZE` | `300000` / `2.0` | Tier 2: $2 bet |
| `TIER3_SIZE` | `3.0` | Tier 3: $3 bet (>$300k holding) |
| `MAX_POSITION_SIZE_USDC` | `2.0` | Max spend per market |
| `MAX_TOTAL_EXPOSURE_USDC` | `12.0` | Max total open exposure |
| `DAILY_LOSS_LIMIT_USDC` | `4.0` | Suspend buys if down this much today |
| `MAX_SLIPPAGE` | `0.05` | Skip order if price moved >5% |
| `ORDER_TYPE` | `market` | `market` or `limit` |
| `POLL_INTERVAL_SECONDS` | `20` | How often to check for new trades |
| `DB_PATH` | `positions.db` | SQLite file path |

Live trading also requires: `POLY_PRIVATE_KEY`, `POLY_FUNDER_ADDRESS`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`.

Telegram (optional): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## External APIs

| API | Base URL | Used For |
|---|---|---|
| Gamma | `https://gamma-api.polymarket.com` | Username → wallet lookup |
| Data | `https://data-api.polymarket.com` | Trade history, current positions |
| CLOB | `https://clob.polymarket.com` | Order placement, last-trade price |

## Database Schema (SQLite)

- `positions` — open positions keyed by `market_id`
- `trade_log` — all executed trades (BUY/SELL/REDEEM)
- `daily_stats` — per-day realized P&L
- `paper_account` — virtual cash balance for paper trading

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values
```

## Deployment

The bot runs on a VPS via Docker Compose. `scripts/ssh_vm.sh` is a helper for SSH access. The `Dockerfile` uses Python 3.12 and the `docker-compose.yml` mounts `.env` and persists `positions.db` via a volume.
