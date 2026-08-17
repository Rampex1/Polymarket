# forecast_edge — handoff

State as of 2026-08-17. `harness.py`'s docstring covers mechanics; this covers
why the thing exists, what has already been ruled out, and what to do next.

## The question

Can a forecaster beat a prediction market's own price by enough to cover the
cost of trading on the difference?

Not "is it accurate". The market price is the number you would be betting
against, so it is the benchmark, and it is a hard one: on 178 resolved
fee-free markets it scored a **Brier of 0.067**, against 0.113 for always
guessing the base rate. The price already carries real information.

## What has already been answered

A **no-retrieval** forecaster (training-cutoff knowledge only, no web access)
was scored against those 178 markets:

| | Brier |
|---|---|
| market price | **0.067** |
| forecaster | 0.156 |
| always the base rate | 0.113 |

Skill **−1.32**, CI on the gain [−0.123, −0.057] — significantly worse than the
price, and worse than not forecasting at all. Trading the disagreements lost
50–65% per bet.

The failure was systematic, not noisy. Every time the model got confident it
was confident in the wrong direction:

| forecaster said | n | market said | actually happened |
|---|---|---|---|
| 0.024 | 36 | 0.101 | 0.056 |
| 0.229 | 42 | 0.139 | 0.143 |
| 0.451 | 26 | 0.223 | **0.115** |
| 0.879 | 15 | 0.308 | **0.333** |

It over-weights dramatic outcomes — 45% where reality was 11.5%, 88% where
reality was 33% — while the market is close to exact in those same buckets.
That is the public-sentiment bias the exercise set out to beat, reproduced
rather than corrected.

**So the open question is narrower than "can an AI forecast":** it is whether
*retrieval* closes a gap of that size. That is what the weekly routine tests.

## Why this is forward-only

A retrieval-enabled forecaster cannot be backtested. Pointed at resolved
markets it can look up what happened, and a lookup is indistinguishable from
skill. The scorer's own control proves the point: an oracle that knows every
answer returns a Brier skill of **+0.999** and is flagged IMPLAUSIBLE — which
is exactly what a spectacular strategy would look like if you were not
checking.

The only defence is writing the prediction down while the answer does not yet
exist. Two rules in `harness.py` enforce it, and they are the reason the
module exists at all:

1. **A prediction is refused once its market has closed.** `--record`
   re-checks Gamma at record time rather than trusting the `--ask` snapshot.
   Verified against live data — a genuinely resolved market is rejected with
   *"has already closed — refusing (that is a backtest)"*.
2. **A prediction is write-once.** Re-recording a market that already has one
   is refused, so a forecast cannot be quietly revised toward the outcome.

`data/forecast_log.db` is tracked in git for the same reason: the code enforces
the rules, and the commit history proves to someone who does not trust the code
that each prediction predates its market's resolution. Do not un-track it, and
do not rewrite history over it.

## Running it

```bash
python -m algorithms.forecast_edge.harness --ask 40      # snapshot open markets
python -m algorithms.forecast_edge.harness --record F    # attach predictions
python -m algorithms.forecast_edge.harness --resolve     # backfill outcomes
python -m algorithms.forecast_edge.harness --score       # Brier vs the market
```

`--ask` writes `questions.json`, which **deliberately contains no prices** —
the forecaster must not see the number it is trying to beat. Anchoring on it
silently invalidates the run and leaves no trace in the data.

Universe is fee-free non-sports markets resolving in 3–30 days with real
volume. Each filter is load-bearing: sports is arbitraged against Pinnacle to
within a point, high-frequency crypto series are coinflips, untraded books
quote phantom midpoints, and a non-zero fee eats a forecasting edge before it
can pay.

## The weekly routine

`trig_01RgFozuSm36im29J1Gypf3a` — Mondays 13:00 UTC, Opus 5, web search on.
Runs resolve → score → ask → forecast → record → commit.

It clones from GitHub, so **anything not pushed does not exist to it.**

## Reading the result

- **The benchmark is the market's Brier, not accuracy.** Being right 90% of the
  time on questions the market priced at 0.95 is losing.
- **Skill above ~0.25 is a red flag, not a win.** A one-week geopolitical
  forecaster does not beat a liquid market by that much. Suspect the agent
  found a price despite the instruction, and check its transcript before
  believing it.
- Beating the price is necessary but not sufficient: the edge still has to
  clear ~0.5% of half-spread before any of it is tradeable.

## Honest expectation

This probably loses. Retrieval has to close a 0.089 Brier gap *and* clear
costs, and the market's own errors in the buckets that matter were ~0. It is
worth running because it costs nothing, the first real numbers land in
September, and it is the one version of the question a backtest cannot answer.

Related: `algorithms/resolution_carry/README.md` is the strategy that *did*
survive measurement, and `calibration.py`'s docstring documents the two
selection traps that make Polymarket backtests lie.
