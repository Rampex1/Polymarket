# resolution_carry

Buy sports moneylines that are already effectively decided, hold them to
settlement, keep the residual. At an ask of 0.97 you pay 97¢ for a contract
that pays $1 — about 3% for waiting.

The claim is **not** that we forecast better than the market. It is the
opposite: we assert the price is *correct*, and that being paid ~3% to sit
through settlement is worth the capital and the tail risk. The counterparty is
usually someone who wants their money back now to put on the next game rather
than wait hours for the oracle. They buy immediacy; we sell it. Hence the
name — carry earned for holding to resolution, the way a bond earns carry to
maturity.

Everything in this strategy follows from one distinction: **decided is not the
same as likely.** A pregame "will there be a goal" market at 0.95 is a
forecast. A match in its 85th minute at 0.95 is a scoreline. Both quote the
same number and they are not the same trade. Only the second is bought here.

## The trade

At an ask of `p` held `d` days:

```
gross return  = (1 - p) / p
annualised    = gross return × 365 / d
break-even    = the market must resolve YES more often than p
```

In the traded band that is 1.5–4.7% gross. The loss is always 100%, so at 0.97
**one loss erases 32 wins.** This is a short-volatility position: it looks
riskless right up until it isn't, and the entire question is whether the tail
arrives more often than `1 - p`. Every risk control below exists because of
that asymmetry.

## The loop

One worker thread, `poll()` every 15 seconds.

### 1. Scan

`MarketDataGateway.top_markets`, paged, volume-ordered, windowed to markets
ending between 6 hours ago and 1 day from now. About 2,100 rows and ~6.5s.

Two properties of Gamma to know:

- A page is capped at 100 rows regardless of `limit`.
- **Any offset ≥ 2100 returns 422**, verified against both `closed=true` and
  `closed=false`. That is a platform ceiling, not the end of our window: more
  than 2,100 markets end within a day, so the scan sees the highest-volume
  2,100 and never the tail. The liquidity floor would reject most of that tail
  anyway, but no claim that this scan is exhaustive should be believed.

The lower bound of the window must track `max_hours_past_end`, or Gamma
filters out post-whistle markets before the screen ever sees them and the
grace window silently does nothing.

### 2. Screen

`screen.py` is pure — no I/O, no clock of its own — so the whole funnel can be
swept offline against archived rows. `evaluate()` returns a `Candidate` or a
string naming the gate that rejected it; the algorithm tallies those into one
log line per poll:

```
Scanned 2100 markets → 0 in band. Rejections: out of band 2088,
   not a winner market 5, pregame 4, not a live event 3
```

That line is the first thing to read when the strategy goes quiet, because it
distinguishes "nothing qualified" from "the scan is broken".

| gate | default | rejection | why |
|---|---|---|---|
| price band | 0.955–0.985 | `out of band` | below is forecasting; above, the residual cannot cover the tail |
| in play | `gameStartTime` passed | `pregame` / `not a live event` | decided, not predicted |
| moneyline | Yes/No + "win" | `not a winner market` | a scoreline is the most legible certainty there is |
| spread | ≤ 0.05 | `spread` | caps how far above the mid we pay |
| liquidity | ≥ $5,000 | `illiquid` | depth proxy (`liquidityClob`) |
| grace window | ≤ 6h past end | `long past end` | the whistle has gone, the oracle has not ruled |
| horizon | ≤ 1 day | `resolves too far out` | capital velocity is the whole point |
| annualised | ≥ 25%/yr | `not worth the wait` | inert at a one-day cap; binds if the window widens |
| category | sports, not crypto/esports | `category` | see below |
| dedupe | held, or signalled < 5 min ago | `already held` / `cooling off` | |

### 3. Rank and size

Survivors are sorted by **annualised return** — return per unit of
capital-time is the point, so 3% in five hours beats 5% in ninety days. The
best few that fit the caps are taken.

Flat `$1` per position, no tiering. Conviction sizing is incoherent when the
thesis is "the price is correct". `max_concurrent_positions` (15) is held
equal to `max_total_exposure_usdc / bet_size_usdc`; `validate()` refuses any
combination where it exceeds what the bankroll can fund, because the excess
would only ever be emitted and rejected.

**One leg per Gamma `eventId`**, applied to a single poll's own picks as well
as to open positions. This is the load-bearing risk control: at 0.97 the loss
is 32× the win, so twenty legs of one event is one position at twenty times
the size. `max_positions_per_category` is inert at 20 — with `require_sports`
on, every primary label is `sports`, so a real value there would cap total
positions rather than cap a theme.

### 4. Enter

An `OpenIntent` with `signal_price = bestAsk` through the shared
`runner.dispatch()`, which runs the risk check, re-fetches the live price, and
refuses on drift over `max_slippage`. Raw observables are logged to `signals`
for later analysis: ask, bid, days to resolution, annualised, liquidity,
category, event id, and `hours_past_end`.

### 5. Exit

Hold to resolution. The shared settle sweep runs every 12 polls and books the
position at the canonical close price once Gamma reports the outcome final.

**There is no stop-loss, deliberately.** A stop realises exactly the losses
this strategy exists to absorb, and at ~3% a win a handful of unnecessary cuts
erases dozens of successes. New lows on held positions are logged instead, so
the question can be settled with data rather than intuition.

## Why the defaults are what they are

Each of these was set by a measurement, and several overturned an assumption
that had looked obvious.

**`require_in_play = True`** — the gate that encodes the thesis. Gamma's
`gameStartTime` is present on 1,871 of 2,100 markets in a one-day window; the
229 without it are not games (weather, tweet counts, index levels) and are
rejected, because a thing that merely expires is never live. `startDate` is
deliberately *not* a fallback: every market has one, so falling back would
report everything as started and silently disable the gate.

**`require_winner_market = True`** needs two conditions, because either alone
leaks. Yes/No outcomes exclude totals (which quote Over/Under) and esports
head-to-heads (which quote team names) — but 682 of 751 live Yes/No sports
markets are props: draws, both-teams-to-score, exact scorelines, tweet counts.
Requiring `win` as a whole word cuts those and leaves one shape, `Will <team>
win on <date>?`. The word boundary matters: it must not match the "Winner" in
an esports "Game 2 Winner".

**`require_sports = True`** earns its place for a different reason than the
one first assumed. Not resolution *timing* — that was measured and sports is
the **less** punctual class (92% of sports markets honoured their stated end
date against essentially 100% for everything else, because games get
postponed). It is that a scoreline and a clock are the most legible source of
certainty available. Weather and index levels are still forecasts at 95%
however punctual their expiry.

**`exclude_categories = ("crypto", "esports")`.** A "will BTC be above X at
4pm" market is a live price that keeps moving until it expires — no decided
outcome waiting on paperwork. A best-of-three at 0.95 is one teamfight from
0.40. Labels are reliable enough for a substring screen: 130/130 crypto and
94/94 esports markets carried theirs, and 0 of 300 sampled markets carried no
labels at all.

**`poll_interval_seconds = 15`.** Across 4,765 archived transits through the
band on tokens that finished ≥0.99, **99% lasted a single one-minute sample**.
The band is open for about a minute, so a 60s poll misses transits outright
whenever one falls between two polls. A scan is ~6.5s, so the effective cycle
is ~21s and a poll never overlaps its own scan.

**`min_hours_to_resolution = 0`.** A floor in hours is a floor on the whole
thesis — the trade is a match decided on the pitch, which is minutes from
settling, not hours. At 6 it left a universe of one sports market. The floor
now applies only ahead of the end date; it is meaningless once the event is
over.

**`max_hours_past_end = 6` — the grace window.** Past the end date is two
populations that a sign test cannot separate. A market whose whistle went
minutes ago is this trade at its purest; a market whose end date passed months
ago is a fossil nobody resolved (106 of 500 rows) whose quote is an artifact.
Measured on prices recorded after the scheduled end:

| band | n | resolved YES | edge | window stays open |
|---|---|---|---|---|
| 0.950–0.980 | 16 | 100% | **+0.035** | median 11m, max 36m |
| 0.980–0.990 | 9 | 100% | +0.015 | median 10m, max 24m |
| 0.990–0.995 | 9 | 100% | +0.008 | median 4m, p90 14h |
| 0.995–1.000 | 40 | 100% | +0.002 | median 42m, max 30h |

The best edge in the dataset is in the ten minutes after a final whistle. 6h
has an order of magnitude of margin at both ends. **The `n` column is the
point, not the 100%**: zero failures in 16 still admits a true failure rate
near 19% against a 3.5% break-even.

**`max_ask_spread = 0.05`**, wider than it looks like it should be. In-play
books run wider than pregame ones, and at 0.02 nothing live ever qualified.
The spread is not a *cost* here — positions are held to resolution and never
sold back across the bid — so this caps how far above the **mid** we will pay.
That premium is the number to watch: a 0.069 spread put the ask 3.4¢ over mid
on a trade returning 2.1¢, which is negative expectancy whenever the mid is
nearer the truth. Anything wider than this should become a relative gate
(`spread ≤ 1 - ask`) rather than a larger constant.

**`min_ask = 0.955`**, not 0.95, is arithmetic. The slippage gate admits a
fill up to `max_slippage` *below* the signal, so a 0.950 signal could fill at
0.9453 — and one did. The floor is meant to bound what we actually own, so it
sits at `0.95 / (1 - max_slippage)`.

**`order_type = "limit"`.** At 3% gross, one tick of slippage is a third of
the return. This posts a GTC limit and accepts non-fills. Whether a GTC at the
ask reliably fills is the biggest execution unknown, and paper cannot answer
it — paper fills at the CLOB price without placing an order.

**`notify_signals = False`.** The channel is a trade log: fills, settlements
and failures only. This screens ~2,100 markets a poll and emits far more
candidates than it fills, so signal and risk-block alerts would bury the
messages that matter. Every candidate is still recorded in `signals`.

## What the universe actually looks like

Worth knowing before reading the funnel as a fault. Of 65 live moneyline
markets in one scan, **33 sat below 0.50 and 32 above 0.985, with nothing in
between** — and all 32 were at 0.999+. A moneyline does not drift, it *steps*:
while the game is competitive it sits mid-range, and the moment it is settled
it snaps to 0.999. It does not linger at 95%.

So this strategy trades the **transit** — the ~1 minute a game spends crossing
0.955→0.985 as it becomes decided. Expect a quiet funnel punctuated by bursts
as games finish, not a steady stream.

## How it loses money

In rough order of expected damage:

1. **Correlated tail.** The most likely way to lose a lot at once. The
   per-event cap is not optional.
2. **Resolution risk.** UMA disputes, abandoned or overturned matches, wording
   that does not say what everyone assumed. Unquantified.
3. **Adverse selection.** Someone is selling at 0.97. Usually they want
   capital back early — that is the edge. Sometimes they know something.
4. **Paying over the mid.** See `max_ask_spread`. A wide book means the ask is
   one seller's opinion, not the market's.
5. **Miscalibration.** If 0.97 markets are right only 94% of the time, this
   loses steadily and looks fine for weeks first.

## Validation status

**Not established.** The strategy has not run long enough to have an opinion,
and the archive cannot substitute:

| to detect a failure rate of | samples needed | samples held |
|---|---|---|
| 3.5% (0.95–0.98) | ~250 | 16 |
| 1% (0.99) | ~900 | 9 |
| 0.1% (0.999) | ~3,000 | 40 |

Every observation so far resolved the obvious way, and that is entirely
consistent both with free money and with a steady bleed. Because the losses
are rare by construction, a strategy that wins 96% and one that wins 99.5%
produce identical-looking months. Only the tail separates them.

**Gates for going live** — all of:

- ≥300 resolved paper positions;
- realised annualised return positive and above the hurdle after fees;
- no single event accounting for more than ~20% of total loss;
- observed loss rate consistent with `1 - mean_entry_ask`.

Do not promote on a short streak. A run of 40 wins is the *expected* start of
both a working strategy and a broken one.

Paper is also structurally blind to two failure modes: whether a resting limit
actually fills inside a ten-minute window, and how much size the book holds at
that price. Those need small real money or a book query.

## Files

```
algorithm.py     poll → scan → screen → rank → OpenIntent; settle sweep for exits
params.py        the whole config surface, with validate()
screen.py        pure: market rows → ranked, diversified candidates. No I/O
seed.py          `python -m` job: seed the price archive with markets resolving soon
calibration.py   `python -m` backtest of the calibration curve against the archive
```

## Operating it

```bash
# what the calibration curve says today
python -m algorithms.resolution_carry.calibration

# widen the archive's universe (the archiver must be running to collect it)
python -m algorithms.resolution_carry.seed --within-days 7

# start this algorithm's paper record over, leaving every other algo alone
PROFILE=experimental python scripts/reset_paper_trade_db.py --algo resolution_carry_paper
```

Tuning is a code edit in `params.py` and a redeploy — profile TOMLs declare
only what runs. The DB partition key is `resolution_carry_paper`; renaming it
orphans the history.

The archive is the only unbiased calibration source available, because the
CLOB drops price history at resolution: anything not archived before a market
settles is gone permanently. The archiver must run with `--fidelity 1`, which
`setup_vm.sh` passes — the default of 60 stores hourly bars, in which a
one-minute transit is invisible.
