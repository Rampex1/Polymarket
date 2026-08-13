# resolution_carry — implementation plan

**Status: built, registered, and running in `experimental.toml` as
`resolution_carry_paper`.** Everything below the Module layout section is
the design that was implemented; read it as the rationale behind the code,
not as pending work. Phase A (calibration) and Phase B (paper) are still
open.

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

## Universe: sports-primary

Sports is the natural home for this strategy, and the reason is capital
velocity. From a live scan of 2,100 open markets, the 0.93–0.985 band by
horizon:

| horizon | n | median annualised |
|---|---|---|
| 6–24h | 2 | **4,958%** |
| 1–3d | 2 | 525% |
| 3–14d | 1 | 161% |
| 14–45d | 7 | 84% |
| >45d | 17 | 19% |

The same 2% trade is worth 250x more at a one-day horizon than a two-month
one. Sports is where short horizons live, and it brings three more things
this strategy specifically needs:

- **Objective resolution.** A scoreboard is not an ambiguous UMA proposal.
  Risk #2 below largely disappears.
- **Free diversification.** Twenty political markets are often one election
  in disguise; Tuesday's games and Wednesday's games are genuinely
  independent, which is what the per-event cap exists to manufacture.
- **Fast evidence.** The paper phase needs several hundred resolved
  positions. Sports produces dozens of resolutions per day, turning a
  year-long validation into a few weeks.

**Only 29 of 2,100 open markets sit in the band at any instant**, and 17 of
those are more than 45 days out. The band is thin as a *stock* and large as a
*flow*: a game that ends decisively passes through 0.93 → 1.00 in its closing
minutes. The strategy is therefore a poller of markets approaching
resolution, not a screener of markets already parked at 0.98.

### The staleness constraint, measured

The counter-argument to in-play sports is that prices move while you are
deciding, and the fills you get are disproportionately the ones that moved
against you. That is measurable, and now measured — how far a market already
at 0.95+ travels over the following interval:

| after | median | p95 |
|---|---|---|
| 1 min | 0.0000 | **0.0045** |
| 5 min | 0.0000 | **0.0185** |
| 15 min | 0.0000 | 0.0260 |

A 0.98 entry pays about 0.020. So at a 5-minute poll, one observation in
twenty has moved by roughly the entire return before we can act; at 15
minutes the p95 move *exceeds* the return. At 1 minute it is a quarter of it.

**This sets `poll_interval_seconds = 60`, not 300** — it is the single
number this measurement changes, and it was worth taking before writing any
strategy code. (Sample is currently ~340 observations over one archiving
session; it will firm up as the archive grows, and the number should be
rechecked before going live.)

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
  seed.py           # `python -m` job: seed the archive with markets resolving soon
  calibration.py    # `python -m` backtest against discovery_archive.db
  PLAN.md           # this file
```

Discovery needed a paged Gamma `/markets` scan, which the read gateway did
not expose: `MarketDataGateway.top_markets` and an `offset` argument on
`api.fetch_top_markets` were added for it. Nothing else in `bot/` changed.

Registration:

All three done: `"resolution_carry"` in `REGISTRY`, a `mode = "paper"` block
in `config/experimental.toml`, and its own `webhook_url` (not insider_flow's
— this one is chatty).

## Parameter surface

Every knob lives in `params.py` with a `_doc` string, per the invariant.

```python
# ── Price band ──────────────────────────────────────────────────────────
min_ask                 0.95   # below this we are forecasting, not carrying
max_ask                 0.985  # above this the residual cannot cover the tail

# ── Time value ──────────────────────────────────────────────────────────
max_days_to_resolution  1      # capital lockup ceiling; a day, not 45 — see below
min_annualized_return   0.25   # inert at a 1-day window; binds again if it widens
min_hours_to_resolution 0      # was 6; see below — it excluded the thesis

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
require_sports          True     # sports-primary; see Universe above
exclude_categories      ()       # deliberately empty — see below
poll_interval_seconds   60       # set by the staleness measurement, not taste
settle_check_every      12
```

Four defaults worth explaining, all deliberate:

**`max_days_to_resolution = 1` and `min_ask = 0.95`**, tightened from 45 days
and 0.93. Both follow from the horizon table above rather than from taste: a
2% carry is worth ~250x more at a one-day horizon than at two months, and
`min_annualized_return` was the wrong instrument for enforcing that — it let
a 31-day trade through on arithmetic. A hard day cap says the same thing
directly, and it makes the gate inert (the weakest trade the band allows
still annualises to ~560%/yr), which is fine: it stays as the binding gate
if the window ever widens.

Two side effects worth knowing. The scan gets much cheaper — a one-day
window is ~400 rows against ~2,100 for 45 days, so it exhausts in about four
pages instead of twenty-one. And the universe becomes almost entirely
in-play: markets resolving within a day are games in progress, which is
where the flow is, but also where a price can move under a resting limit.


**`min_hours_to_resolution = 0`, changed from 6 after the first live scan.**
The 6-hour floor was written to avoid stale books and settling-right-now
noise, and it quietly excluded the strategy's whole reason to exist. The
trade described in the Universe section — a match decided on the pitch,
sitting at 0.97 while it waits to settle — is *minutes* from resolution, not
hours. Six hours before a game nobody knows who wins, so the price is not in
the band yet. Measured across all 2,100 markets Gamma will serve: 46 in band
with a usable spread, exactly **one** of them sports, and that one 31 days
out. The floor and the sports-primary universe could not both hold.

At 0 the gate still rejects a market whose end date has *passed*, which is
the part that was actually load-bearing: trading has effectively stopped,
so the quote is a dead book and there is no carry left to earn. `validate()`
refuses a negative value for that reason. Annualisation still floors the
horizon at one day, so a ten-minute wait is not reported as a four-figure
return.


**`order_type = "limit"`.** At 2% gross, one tick of slippage is a quarter of
the return and two ticks is half. A market/FOK order that fills at 0.99
instead of 0.98 halves the trade. This strategy must post a GTC limit at its
target price and accept non-fills. That in turn means unfilled orders need
reconciliation — an open GTC that never fills must not be mistaken for a
position. **This is the biggest execution unknown and should be verified in
paper before live.**

**`exclude_categories = ()` with `require_sports = True`.** Every other
strategy screens sports *out*, because efficient pricing destroys a
forecasting edge. Here efficiency is *the product* — we need the price to be
right, and we are paid for waiting rather than for knowing better. A match
decided on the pitch and sitting at 0.98 awaiting settlement is the ideal
trade. `require_sports` is a switch rather than a hard-coding so the
non-sports arm can be run as a control; log `market_category` either way.

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

Reject if `days > max_days_to_resolution`, the end date has already passed,
`hours < min_hours_to_resolution` (0 by default — see above), or
`annualised < min_annualized_return`. **Unknown end date fails closed** — an
un-priceable wait cannot clear a time-value hurdle. Use `api.market_end_ts`.

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

**Both the seeder and the backtest are built.** Status:

```bash
python -m algorithms.resolution_carry.seed --within-days 7   # widen the universe
python -m algorithms.resolution_carry.calibration            # read the verdict
```

- Archiver is **running** in tmux session `archiver`, `--loop --every 1800
  --fidelity 1`, logging to `logs/archiver.log`.
- Archive is **seeded** with 91 markets resolving within 7 days, 42 of them
  sports. Re-run the seeder periodically; markets resolve and the universe
  needs refreshing.
- **Fidelity is in minutes, and it matters.** The archiver's default of 60
  produces *hourly* bars, which cannot answer the staleness question at all —
  a football match yields two data points. It now runs at `--fidelity 1`
  (~60s bars). If it is ever restarted, keep that flag or this whole phase
  goes blind.

Current output is real but far too thin to act on: 288 resolved tokens, 214
independent observations, and single-digit `n` in the bands that matter. One
row is worth staring at anyway — of two markets priced ~0.976 with under six
hours left, **one lost**. n=2 means nothing, but it is a concrete picture of
the tail this strategy is short.

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
