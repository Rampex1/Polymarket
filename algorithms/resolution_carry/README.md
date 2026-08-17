# resolution_carry

Buy sports moneylines that are already effectively decided, hold them to
settlement, keep the residual. At an ask of 0.985 you pay 98.5¢ for a contract
that pays $1 — about 1.5% for waiting.

The claim is **not** that we forecast better than the market. It is the
opposite: we assert the price is *correct*, and that being paid ~1.5% to sit
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

In the traded band that is 1.0–2.0% gross. The loss is always 100%, so at 0.985
**one loss erases 65 wins.** This is a short-volatility position: it looks
riskless right up until it isn't, and the entire question is whether the tail
arrives more often than `1 - p`. Every risk control below exists because of
that asymmetry.

That question is no longer open. Measured across **17,557 closed moneylines** —
every one in a 25-day window, so nothing is conditioned on who traded them —
one entry per market at its first in-play touch of each ask band, net of a 1c
spread and the 5%·p·(1−p) taker fee:

| ask band | n | failures | failure rate | 95% upper | break-even | net ROI |
|---|---|---|---|---|---|---|
| 0.950–0.960 | 623 | 35 | 5.62% | 7.71% | 4.73% | **−1.17%** |
| 0.960–0.970 | 559 | 20 | 3.58% | 5.46% | 3.69% | −0.06% |
| 0.970–0.980 | 551 | 16 | 2.90% | 4.66% | 2.63% | −0.42% |
| **0.980–0.990** | **627** | **4** | **0.64%** | 1.63% | 1.54% | **+0.84%** |
| 0.990–0.995 | 483 | 4 | 0.83% | 2.11% | 0.80% | −0.07% |

The band this strategy used to trade (ask 0.955–0.985) priced to **−0.01%**
over n=1,529 — a coin flip that pays its own costs, which is exactly the live
30–3 / −$2.15 record it produced. The edge is entirely above 0.975: over
0.975–0.990 the failure rate is 0.82% against a 1.78% break-even, and the 95%
upper bound (1.68%) sits *below* break-even, so it is established rather than
merely favourable.

## The loop

One worker thread, `poll()` every 15 seconds.

### 1. Scan

`MarketDataGateway.top_markets`, paged, volume-ordered, windowed to markets
ending between 6 hours ago and 1 day from now. About 2,100 rows and ~6.5s.

Pages are **screened as they arrive** rather than accumulated. Gamma rows are
fat — nested events, tags, outcomes — and holding the whole window at once
peaked at 34.5 MB per poll against 5.7 MB page-at-a-time, every 15 seconds, on
a 498 MB box that also runs the archiver and the Discord bot.

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
| price band | 0.980–0.990 | `out of band` | below 0.975 the measured failure rate exceeds the residual; above 0.990 the residual cannot cover the tail |
| in play | `gameStartTime` passed | `pregame` / `not a live event` | decided, not predicted |
| moneyline | Yes/No + "win" | `not a winner market` | a scoreline is the most legible certainty there is |
| spread | ≤ 0.015 | `spread` | the edge is 1–2c wide, so a wider book erases it |
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
as to open positions. This is the load-bearing risk control: at 0.985 the loss
is 65× the win, so twenty legs of one event is one position at twenty times
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
this strategy exists to absorb, and at ~1.5% a win a handful of unnecessary cuts
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

**`max_ask_spread = 0.015`**, and this one replaced a judgement with a
measurement. The old value was 0.05, on the reasoning that in-play books run
wider than pregame ones and that the spread is not a *cost* when you hold to
resolution. Both halves are true and the conclusion was still wrong: re-running
the 0.975–0.990 band against wider assumed spreads gives **+1.14%** at 0.005,
**+0.89%** at 0.01, **+0.40%** at 0.02 and **−0.07%** at 0.03. The whole edge is
one to two cents wide, so a 5c cap admitted exactly the books that erase it.
In-play moneylines at 0.98+ *with real depth* quote a 0.005 median spread (0.009
at p75) — the earlier finding that "at 0.02 nothing live ever qualified" was
measured without a depth filter, on books that were two lonely orders. 0.015 is
roughly the `spread ≤ 1 - ask` relative gate this section used to recommend,
written as the constant the band implies.

**`min_ask = 0.980`**, and the arithmetic that used to justify 0.955 now
justifies this. The slippage gate admits a fill up to `max_slippage` *below*
the signal, so the floor has to sit at `floor / (1 - max_slippage)` for the
worst admissible fill to land on the right side of it. What changed is the
floor itself: it was 0.95 by assertion, and it is 0.975 by measurement, because
below 0.975 the observed failure rate exceeds the residual (see the table
above). 0.975 / 0.995 ≈ 0.980.

**`order_type = "limit"`.** At 1.5% gross, one tick of slippage is most of
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
0.980→0.990 as it becomes decided. Expect a quiet funnel punctuated by bursts
as games finish, not a steady stream.

## The underdog A/B

`config/experimental.toml` runs two blocks over the same screen, differing
only in `buy_underdog` via `VARIANTS`:

| name | side | entry | must be right |
|---|---|---|---|
| `resolution_carry_paper` | favourite | ~0.985 | 98.5% |
| `resolution_carry_underdog_paper` | underdog | ~0.04–0.06 | 4–6% |

**The underdog arm is pinned to the old band** (`min_ask` 0.955, `max_ask`
0.985, `max_ask_spread` 0.05, all set in `VARIANTS`). Letting it follow the
control's new floor would have moved it from buying 0.04–0.06 underdogs to
buying 0.01–0.02 ones — a different experiment from the one that was started,
and a worse one. Pinning keeps the arm comparable to its own history.

**The measurement has largely answered its question.** Across n=1,529 in that
old band the favourite failed 3.01% of the time against a 3.15% break-even, so
the mirror is a break-even-to-negative trade, not the 9.1% hit rate the first
33 settlements suggested. The arm is worth leaving on only long enough for its
own sample to agree; it is not worth waiting 200 settlements for.

Every gate and the ranking run on the favourite regardless of side, so the
arms select the same markets and differ only in the token bought. Two knobs
move with the side because they are relative, not because they are tuned:
`max_slippage` (0.005 of a 0.04 entry is a fifth of a cent — 0.12 restores
the control's absolute tolerance) and `daily_loss_limit_usdc` (at a ~9% hit
rate, $5 suspends the arm within an hour every day).

**Why it is being tested.** Over the control's first 33 settlements it went
30–3 for −$2.15 — a 90.9% win rate against a 97.2% break-even. The mirror of
those exact trades would have made **+$40.05**, on a 9.1% hit rate against a
4.1% break-even.

**Why that is not a green light.** Both results rest on the same three
events. The 95% interval on 9.1% is [3.1%, 23.6%], which contains break-even,
and a perfectly fair market throws 3+ winners 15% of the time. At a 4¢ entry
the estimate is ~25× levered, so a few points of error is the difference
between +2,000% and −25%. It also runs against the favourite-longshot bias,
where longshots are usually *over*priced. ~200 settlements separate the two.

Note the underdog's ask is `1 - favourite bid`, never `1 - favourite ask`:
the spread is paid on whichever side is taken. A 0.96/0.94 book makes the
underdog 0.06, not 0.04 — half the theoretical edge gone before anything
happens.

Two caveats on reading the result. The arms poll independently ~8s apart, so
in production they do **not** trade an identical market set — the band is a
transit and one arm can see a market the other misses. And this arm inverts
the thesis: the control asserts the price is correct and collects a fee for
waiting, while this asserts the price is wrong at the tail, which is a
forecasting claim of the kind that sank copy_trade.

Split by the `side` field in each `signals` row.

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

**Established offline, not yet live.** The band is now backed by 17,557
closed moneylines rather than by the handful of archived observations this
section used to apologise for: over ask 0.975–0.990, 7 failures in 857 entries
(0.82%) against a 1.78% break-even, with the 95% upper bound at 1.68% — below
break-even, which is what makes it a result rather than a hope.

Two caveats keep it from being a licence.

- **The offline sample buys at the ask and always fills.** Paper cannot answer
  whether a resting GTC limit fills inside the window, and that is the single
  largest remaining unknown.
- **A 25-day window is one slice of one season.** Split in half by date the
  band gives 1 failure in 428 and 6 in 429 — consistent with a common 0.8% rate
  under Poisson, but not a demonstration of stability across regimes.

Because the losses are rare by construction, a strategy that wins 98.5% and one
that wins 99.4% still produce identical-looking months. Only the tail separates
them, so the live gates below stand unchanged.

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
