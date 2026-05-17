# Running the Polymarket Copy-Trading Bot

## Prerequisites

- Python 3.11+
- A Polymarket account funded with USDC on Polygon (needed for Phase 2+)

---

## Setup

**1. Create and activate a virtual environment**
```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Configure environment variables**
```bash
cp .env.example .env
```

Then open `.env` and fill in `TARGET_ADDRESS` (see below).

---

## Finding surfandturf's Wallet Address

Polymarket's username lookup API requires authentication, so the address must be found manually:

1. Go to [polymarket.com](https://polymarket.com) and search for **surfandturf**
2. Click their profile — the page URL will become:
   ```
   https://polymarket.com/profile/0xABC123...
   ```
3. Copy the `0x...` address and paste it into `.env`:
   ```
   TARGET_ADDRESS=0xABC123...
   ```

---

## Running the Bot

```bash
python main.py
```

On startup the bot will:
1. Confirm the wallet address is set
2. Print the last 5 trades from that address (confirms the feed is live)
3. Enter a polling loop, printing any new trade above $50

---

## Configuration

All settings are in `config.py` and can be overridden via `.env`:

| Variable | Default | Description |
|---|---|---|
| `TARGET_ADDRESS` | _(required)_ | surfandturf's proxy wallet address |
| `POLL_INTERVAL_SECONDS` | `20` | How often to check for new trades |
| `MIN_TRADE_SIZE_USDC` | `50` | Ignore trades smaller than this |

---

## Project Structure

```
.
├── main.py          # Entry point
├── fetcher.py       # Wallet lookup + trade polling
├── models.py        # Trade dataclass
├── config.py        # Settings
├── requirements.txt
├── .env.example     # Template for environment variables
├── plan.md          # Full build plan
└── instructions.md  # This file
```
