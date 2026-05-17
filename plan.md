# Polymarket Copy-Trading Bot — Plan

## Overview

Monitor a profitable Polymarket user (surfandturf) and automatically mirror their trades at a scaled-down size.

```
[Polymarket API] → [Trade Monitor] → [Signal Processor] → [Order Executor] → [Position Tracker]
```

---

## Phase 1: Data Layer — Monitor surfandturf's Activity

**Data sources:**
- **Polymarket CLOB API** — real-time order book and trade history
- **Polymarket Gamma API** — market metadata, positions, user activity
- **The Graph subgraph** — on-chain trade history on Polygon (source of truth)

**Key task:** Poll or stream surfandturf's wallet address for new trades. Their wallet address can be found by looking up their profile on Polymarket's frontend and inspecting the API calls, or via the subgraph.

---

## Phase 2: Trade Detection

When a new trade is detected, extract:
- Market ID + question
- Side (YES/NO)
- Size (in USDC)
- Entry price (probability)
- Whether it's opening or closing a position

**Signal filter:** Ignore trades below a threshold size (e.g., < $50) to skip noise.

---

## Phase 3: Trade Execution

- Use **`py-clob-client`** (Polymarket's official Python SDK) to place orders
- Scale the trade size proportionally (e.g., if surfandturf bets $1000, you bet $50 — 5% scale)
- Support both **market orders** (immediate fill) and **limit orders** (better price, risk of no fill)

---

## Phase 4: Position & Risk Management

- Track open positions vs. theirs
- Mirror closes: when they exit, you exit
- Hard limits: max position size, max total exposure, daily loss limit
- Slippage guard: skip if market has moved >X% since their trade

---

## Phase 5: Infrastructure

| Component | Choice |
|---|---|
| Language | Python |
| Scheduling | APScheduler or a simple polling loop |
| Data store | SQLite (simple) or Postgres |
| Notifications | Telegram bot or email for trade alerts |
| Hosting | VPS (Railway, Fly.io, or DigitalOcean) |

---

## Implementation Order

1. Find surfandturf's wallet address via Polymarket's API
2. Build the trade fetcher — query their history and detect new trades
3. Build the executor — connect your Polymarket account via `py-clob-client`, fund with USDC on Polygon
4. Add the scaling logic — proportional sizing
5. Add position tracking — know what you're in
6. Add risk controls — caps and filters
7. Run paper-trade first — log what you *would* have done without executing, verify correctness

---

## Key Risks to Design For

- **Latency**: By the time you copy, price may have moved. Set a max slippage tolerance.
- **Liquidity**: Smaller markets may not have enough liquidity at their price.
- **Tail risk**: Their strategy may have a bad period — set a max drawdown kill switch.
- **API rate limits**: Polymarket rate-limits the CLOB API; poll responsibly (~10–30s intervals).
