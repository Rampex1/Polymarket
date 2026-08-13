# resolution_carry — implementation plan

**Status: design only. No code, not registered, does not run.**

## Thesis

Buy outcomes the market has already priced as near-certain, hold to
resolution, collect the residual. At an ask of 0.98 you pay 98¢ for a
contract that pays $1, so the trade returns ~2% if the market is right.

This is not forecasting. We are not claiming to know better than the price —
we are claiming the price is *correct* and that being paid ~2% to wait for
settlement is worth the capital and the tail risk. The counterparty is
typically someone who wants their money back before resolution and will pay
to get out early. The name reflects that: we earn **carry** for holding to
**resolution**, the way a bond earns carry to maturity.

## Why this rather than copy_trade

copy_trade is blocked on a problem we measured and could not solve: selection
of profitable wallets does not generalise out of sample (holdout rank
correlation −0.21, lift flipping sign with cohort size). Its signal depends
on identifying skill in other people, and we cannot do that reliably from
public data.

This strategy has no such dependency:

- **The signal is mechanical.** Price, time to resolution, liquidity. No
  judgement about who is smart.
- **It is backtestable.** `data/discovery_archive.db` holds real price
  history, and resolution lookups work again as of the Gamma `closed=true`
  fix. A calibration curve can be computed offline before any money moves.
- **Fees do not eat it.** Measured across 246 real positions, entry fees
  scale with *potential winnings*, not notional:

  | entry price | median fee (% of cost) | gross return |
  |---|---|---|
  | 0.95–1.00 | **0.040%** | 2.6% |
  | 0.70–0.90 | 0.870% | 25.0% |
  | 0.40–0.70 | 1.470% | 81.8% |
  | 0.00–0.40 | 2.648% | 400% |

  At the top of the book the fee is ~1.5% of the gross return. Everywhere
  else it is 6–20%. This strategy lives in the one price band where friction
  is negligible, which is not a coincidence — it is why the band is
  contested.

## The economics

At an ask of `p`, held `d` days:

```
gross return   = (1 - p) / p
annualised     = gross return × 365 / d
break-even     = the market must resolve YES more often than p
```

| ask | gross | needs to be right | 30d annualised | 180d annualised |
|---|---|---|---|---|
| 0.99 | 1.0% | >99.0% | 12% | 2% |
| 0.98 | 2.0% | >98.0% | 25% | 4% |
| 0.95 | 5.3% | >95.0% | 64% | 11% |
| 0.93 | 7.5% | >93.0% | 92% | 15% |

Two things fall out immediately:

1. **Time is the binding constraint, not price.** A 2% return is excellent
   over 30 days and worse than cash over 180. The time-value gate is the
   most important knob in the strategy, not the price band.
2. **The loss is 49× the win at 0.98.** One miss erases 49 successes. This
   is a short-volatility profile: it looks riskless right up until it isn't,
   and the entire question is whether the tail arrives more often than `1-p`.

## What we know, and what we do not

**Measured this session:**

- Fee structure by price band (table above).
- From `discovery_archive.db`, of 80 archived tokens with final outcomes:
  27/27 that ever touched ≥0.99 resolved YES, 8/8 in 0.95–0.99, 0/35 below
  0.50.

**Not established, and the strategy depends on all of it:**

- **True calibration conditioned on time.** The archive figure uses *peak
  price ever reached*, which is survivor-flavoured — a market that spiked to
  0.96 and collapsed still counts as having reached it. The question that
  matters is: *priced at p with d days remaining, what fraction resolve
  YES?* That needs the timestamped series, not the max.
- **Sample size.** n=8 in the 0.95–0.99 band cannot distinguish 98% from
  100%, and the whole strategy lives in that gap.
- **Depth at the top of the book.** Unknown whether meaningful size can be
  filled at the ask without moving it.
- **Resolution risk.** Frequency of markets that resolve against a
  near-certain price on a wording technicality or a disputed UMA proposal.

Nothing here should be traded live until the calibration question has a real
answer. Paper is how we get one.

## Module layout

Follows the repo convention: `params.py` is the entire config surface, pure
screening logic is separated from I/O so it can be swept offline, and
operational jobs are package modules run with `python -m`.

```
algorithms/resolution_carry/
  __init__.py       # exports ResolutionCarryAlgorithm, ResolutionCarryParams
  algorithm.py      # poll → screen → OpenIntent; settle sweep for exits
  params.py         # ResolutionCarryParams — pure schema + validate()
  screen.py         # pure: market rows → ranked candidates. No I/O.
  calibration.py    # `python -m` backtest against discovery_archive.db
  PLAN.md           # this file
```

Registration, once the code exists:

1. `algorithms/__init__.py` → add `"resolution_carry"` to `REGISTRY`.
2. `config/experimental.toml` → a `[[algorithm]]` block, `mode = "paper"`.
3. `params.py` → a real `webhook_url` (`validate()` rejects an empty one;
   do not reuse insider_flow's, this one will be chatty).

## Parameter surface

Every knob lives in `params.py` with a `_doc` string, per the invariant.

```python
# ── Price band ──────────────────────────────────────────────────────────
min_ask                 0.93   # below this we are forecasting, not carrying
max_ask                 0.985  # above this the residual cannot cover the tail

# ── Time value ──────────────────────────────────────────────────────────
max_days_to_resolution  45     # capital lockup ceiling
min_annualized_return   0.25   # the real hurdle: 2% in 30d passes, 2% in 180d does not
min_hours_to_resolution 6      # avoid settling-right-now noise and stale books

# ── Liquidity ───────────────────────────────────────────────────────────
min_market_liquidity_usdc  5000   # Gamma `liquidityClob`, proxy for depth
max_ask_spread             0.02   # bestAsk - bestBid; a wide book means no real price

# ── Diversification (the load-bearing risk control) ─────────────────────
max_concurrent_positions   20
max_positions_per_event     1     # one leg per Gamma eventId
max_positions_per_category  5     # cap correlated themes

# ── Sizing ──────────────────────────────────────────────────────────────
bet_size_usdc              1.0
max_total_exposure_usdc   15.0
daily_loss_limit_usdc      5.0
min_order_size_usdc        1.0

# ── Execution ───────────────────────────────────────────────────────────
order_type              "limit"  # NOT market — see below
max_slippage            0.005    # half a cent is a quarter of the return

# ── Screening ───────────────────────────────────────────────────────────
exclude_categories      ()       # deliberately empty — see below
poll_interval_seconds   300
settle_check_every      12
```

Two defaults that differ from the other strategies, both deliberate:

**`order_type = "limit"`.** At 2% gross, one tick of slippage is a quarter of
the return and two ticks is half. A market/FOK order that fills at 0.99
instead of 0.98 halves the trade. This strategy must post a GTC limit at its
target price and accept non-fills. That in turn means unfilled orders need
reconciliation — an open GTC that never fills must not be mistaken for a
position. **This is the biggest execution unknown and should be verified in
paper before live.**

**`exclude_categories = ()`.** Every other strategy screens out sports
because efficient pricing destroys a forecasting edge. Here efficiency is
*the product* — we want the price to be right. A finished match sitting at
0.98 awaiting settlement is the ideal trade: objectively determined, short
wait, no information asymmetry left to exploit. Sports should be *included*
and its performance tracked separately in the signals features.

## Pipeline

### 1. Candidate discovery

`api.fetch_top_markets(closed=False, limit=..., end_date_max=<now + max_days>)`
already sorts by `volumeNum` and takes a date window, which does most of the
work. Page it for coverage.

Filter each row on:

- `bestAsk` within `[min_ask, max_ask]` — **use the ask, not `lastTradePrice`
  or the mid.** The ask is what we would actually pay; the last trade may be
  stale and the mid is unfillable.
- `bestAsk - bestBid <= max_ask_spread`.
- `liquidityClob >= min_market_liquidity_usdc`.

### 2. Time-value gate

Reuse the shape of `insider_flow`'s gate — the formula is identical and
already proven:

```python
days       = (end_ts - now) / 86_400
win_return = (1 - ask) / ask
annualised = win_return * 365 / max(days, 1)
```

Reject if `days > max_days_to_resolution`, `hours < min_hours_to_resolution`,
or `annualised < min_annualized_return`. **Unknown end date fails closed** —
an un-priceable wait cannot clear a time-value hurdle. Use
`api.market_end_ts`.

### 3. Diversification gate

The one that actually prevents ruin. Twenty positions at 0.98 that are all
legs of the same election are one position at 0.98 with twenty times the
size. Before opening:

- reject if we already hold a position in this `eventId`
  (`market["events"][0]["id"]`);
- reject if positions sharing this market's primary Gamma category already
  number `max_positions_per_category` (use `api.market_labels`).

### 4. Ranking and sizing

Survivors are ranked by **annualised return** — the return per unit of
capital-time is the whole point, so a 2% trade resolving in 5 days beats a
5% trade resolving in 90. Take the best N that fit within
`max_concurrent_positions` and `max_total_exposure_usdc`. Flat `bet_size_usdc`
per position; no tiering. Conviction sizing makes no sense when the thesis is
"the price is correct".

### 5. Entry

`OpenIntent` with `signal_price = bestAsk`, through the existing
`runner.dispatch()` — risk check, slippage gate, order, ledger write,
notification. `features` should log raw observables for later analysis:
`ask`, `bid`, `days_to_resolution`, `annualised`, `liquidity`,
`market_category`, `event_id`.

### 6. Exit

**v1: hold to resolution.** The settle sweep does it, and as of the Gamma
`closed=true` fix it actually works — before that, no position in this repo
could ever settle.

No stop-loss in v1, deliberately. A stop realises exactly the losses this
strategy is designed to absorb, and at 2% per win a handful of unnecessary
cuts erases dozens of successes. But **log the drawdown** on every position
so the question can be answered with data: if paper shows that positions
which fall below ~0.5 essentially never recover, a catastrophic stop becomes
justified. Do not build it on intuition.

## How this loses money

In rough order of expected damage:

1. **Correlated tail.** The single most likely way to lose a lot at once.
   Mitigated by the per-event and per-category caps, which are therefore not
   optional.
2. **Resolution risk.** UMA disputes and ambiguous wording can resolve a
   99%-certain market the other way. Unquantified. Paper will surface the
   base rate; markets with unusual `resolutionSource` may deserve a filter.
3. **Adverse selection.** Someone is selling at 0.98. Usually they want
   capital back early — that is our edge. Sometimes they know something. The
   2% is compensation for not knowing which.
4. **Execution slippage.** Structural, not incidental: one tick is a quarter
   of the return. This is why `order_type = "limit"`.
5. **Capital lockup.** Money parked in a 45-day carry is not compounding.
   The `min_annualized_return` hurdle is what stops this being a slow bleed
   against just holding cash.
6. **Miscalibration.** If 0.98 markets are actually right only 96% of the
   time, the strategy loses steadily and looks fine for weeks first. This is
   the one that the paper run exists to detect, and the reason for the
   sample-size discipline below.

## Validation plan

### Phase A — backtest before writing strategy code

`calibration.py`, a `python -m` job over `discovery_archive.db`:

> For each archived token, at each snapshot `ts` with price `p` and `d` days
> until its market's end date, record `(p, d, final_outcome)`. Report the
> resolved-YES rate bucketed by `(price band × days-remaining band)`.

This is the honest version of the peak-price number above and directly
answers whether the premise holds. Two prerequisites:

- **Restart the archiver.** It has been dark since 2026-06-11 and holds only
  123 markets. `python -m algorithms.insider_flow.archive --loop --every 3600`.
- **Seed it with high-priced markets.** It currently tracks what discovery
  happens to surface, which is not the 0.93–0.99 band this strategy needs.
  Its `tracked_markets` table takes arbitrary condition ids, so a screen for
  high-ask markets can feed it directly.

The archive is the only unbiased calibration source available: the CLOB drops
price history at resolution, so anything not archived before it settles is
gone forever. Every week the archiver is off is a week of this dataset lost.

### Phase B — paper

Run in `experimental.toml` alongside the existing paper algorithms. The
metric that matters is **realised return per capital-day**, annualised — not
win rate. A 98% win rate is expected and says nothing on its own; the
question is whether the accumulated 2% wins exceed the occasional 98% loss.

**Sample size discipline.** At a true 98% rate you see one loss per 50
positions. To pin the loss rate to ±1% takes roughly:

```
σ = sqrt(0.98 × 0.02) ≈ 0.14      n ≈ (1.645 × 0.14 / 0.01)² ≈ 530
```

So **several hundred resolved positions** before the paper result means
anything. At a handful of entries per day that is months. Interim results
will look great — a run of 40 wins is the *expected* start of both a working
strategy and a broken one. Do not promote to live on a short streak; that is
precisely the error the copy_trade holdout was built to catch.

### Phase C — gates for going live

All of:

- ≥300 resolved paper positions;
- realised annualised return positive and above the `min_annualized_return`
  hurdle after fees;
- no single event or category accounting for more than ~20% of total loss;
- the observed loss rate consistent with `1 - mean_entry_ask` (i.e. the
  market is calibrated, which is the whole premise).

## Open questions to settle during implementation

- Does a GTC limit at the ask reliably fill on Polymarket, and how does the
  runner report a partial or unfilled GTC? Non-fills must not be recorded as
  positions.
- Is `liquidityClob` a usable depth proxy, or is a CLOB book query needed?
- Should the price band be dynamic — e.g. require a higher `min_ask` for
  longer-dated markets, since more time means more chance to be wrong?
- Negative-risk events: buying several NO legs may be safer or may be
  correlated exposure in disguise. Needs thought before allowing it.
