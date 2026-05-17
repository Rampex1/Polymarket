# Polymarket Copy-Trading Bot

Automatically mirrors the trades of a profitable Polymarket user ([surfandturf](https://polymarket.com)) at a scaled-down size. Supports paper trading, live execution, Telegram notifications, and persistent position tracking.

## How it works

1. **Monitor** — polls surfandturf's trade activity every 20 seconds via Polymarket's data API
2. **Detect** — filters for real `TRADE` events above a minimum size, ignoring redeems and rebates
3. **Scale** — sizes each order as a fraction of the signal, capped to a percentage of available balance
4. **Risk check** — enforces per-position, total exposure, and daily loss limits before every order
5. **Execute** — places a market (FOK) or limit (GTC) order via Polymarket's CLOB API
6. **Track** — records positions and P&L in a local SQLite database
7. **Notify** — sends Telegram messages on every trade event and a daily portfolio summary

## Project structure

```
├── bot/
│   ├── config.py       # All settings, loaded from .env
│   ├── models.py       # Trade dataclass
│   ├── db.py           # SQLite connection and schema
│   ├── fetcher.py      # Trade monitor and poll loop
│   ├── executor.py     # Order sizing, slippage guard, CLOB submission
│   ├── positions.py    # Position tracker and risk manager
│   └── notifier.py     # Telegram alerts and daily summary thread
├── scripts/
│   └── setup_keys.py   # One-time CLOB API key generator
├── docs/
│   └── instructions.md
├── main.py             # Entry point
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Prerequisites

- Python 3.11+
- A Polymarket account funded with USDC on Polygon (live trading only)

## Setup

**1. Clone and create a virtual environment**
```bash
git clone https://github.com/Rampex1/Polymarket.git
cd Polymarket
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Configure environment variables**
```bash
cp .env.example .env
```

Open `.env` and fill in the values. At minimum, set `TARGET_ADDRESS` (see below).

**3. Find surfandturf's wallet address**

Polymarket's username API requires authentication, so the address must be set manually:

1. Go to [polymarket.com](https://polymarket.com) and find the user
2. Click their profile — the URL becomes `https://polymarket.com/profile/0xABC...`
3. Copy the address into `.env`:
   ```
   TARGET_ADDRESS=0x9f2fe025f84839ca81dd8e0338892605702d2ca8
   ```

## Running

**Paper trade (safe default)**
```bash
python main.py
```

Paper mode is on by default. The bot logs every order it *would* place without touching the exchange.

**Live trading**

Generate CLOB API keys first (one-time):
```bash
POLY_PRIVATE_KEY=0x... python scripts/setup_keys.py
```

Add the output to `.env`, then set:
```
PAPER_TRADE=false
POLY_PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...   # your proxy wallet address
```

## Configuration

All values can be overridden in `.env`.

| Variable | Default | Description |
|---|---|---|
| `TARGET_ADDRESS` | surfandturf's address | Wallet to copy |
| `PAPER_TRADE` | `true` | Log-only mode; set to `false` for live trading |
| `PAPER_STARTING_BALANCE` | `20.0` | Virtual USDC balance for paper trading |
| `SCALE_FACTOR` | `0.05` | Fraction of surfandturf's trade size to copy |
| `MAX_TRADE_PCT` | `0.20` | Max spend per trade as a fraction of available balance |
| `ORDER_TYPE` | `market` | `market` (FOK, immediate) or `limit` (GTC at signal price) |
| `MIN_TRADE_SIZE_USDC` | `50` | Ignore surfandturf trades below this size |
| `MIN_ORDER_SIZE_USDC` | `0.50` | Skip if scaled order is below this floor |
| `MAX_SLIPPAGE` | `0.05` | Skip trade if price moved more than 5% since signal |
| `MAX_POSITION_SIZE_USDC` | `6.0` | Max USDC cost per market position |
| `MAX_TOTAL_EXPOSURE_USDC` | `20.0` | Max total USDC across all open positions |
| `DAILY_LOSS_LIMIT_USDC` | `5.0` | Stop new buys if today's realized loss exceeds this |
| `TELEGRAM_BOT_TOKEN` | — | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | — | Your Telegram chat ID |
| `POLL_INTERVAL_SECONDS` | `20` | How often to check for new trades |

## Telegram notifications

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token
2. Start a chat with your new bot
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`
4. Add both to `.env`

The bot sends alerts for: signal detected, order placed (with P&L on sells), risk blocks, slippage skips, startup/shutdown, and a daily portfolio summary at midnight.

## Deploying on a VPS

```bash
# Copy the repo to your server, then:
cp .env.example .env   # fill in all values
docker compose up -d   # build and start
docker compose logs -f # tail logs
```

`positions.db` is bind-mounted from the host so trade history survives container rebuilds.

## Risk disclaimer

This bot copies trades automatically. Past performance of any trader is not indicative of future results. Never risk more than you can afford to lose. Always paper trade first and verify the bot behaves as expected before enabling live execution.
