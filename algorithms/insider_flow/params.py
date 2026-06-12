"""
Configuration for the insider-flow algorithm.

Env naming: every knob is `INSIDERFLOW_<NAME>`. Unlike copy_trade there is
no legacy fallback — this algorithm is new.

Every field reads its env var through `default_factory` so the environment
is re-evaluated per instantiation (matters for tests, and means a profile
can construct params before/after dotenv loading without import-order traps).
"""

import os
from dataclasses import dataclass, field

from bot.algorithm import Mode
from bot.config import env_value

_P = "INSIDERFLOW_"


def _env(name: str, default: str) -> str:
    return env_value(_P + name, default=default)


def _default_mode() -> Mode:
    return Mode(_env("MODE", "paper").lower())


def _default_exclude_titles() -> tuple:
    """Comma-separated env override; default screens out the sports firehose.

    Caveat: " vs " also matches head-to-head political markets ("Trump vs.
    Newsom…"). Acceptable for v1 — sports dominates the big-cash feed by an
    order of magnitude. Clear INSIDERFLOW_EXCLUDE_TITLES to disable.
    """
    raw = os.getenv(_P + "EXCLUDE_TITLES")
    if raw is not None and raw != "":
        return tuple(p for p in raw.split(",") if p)
    return (" vs. ", " vs ", "O/U", "Spread")


def _default_exclude_categories() -> tuple:
    """Gamma category/tag substrings to reject (lowercased). The authoritative
    sports screen — title patterns miss formats like "Will <team> win on
    <date>?", but Gamma tags those markets Sports. "sports" also matches
    "esports" by substring."""
    raw = os.getenv(_P + "EXCLUDE_CATEGORIES")
    if raw is not None and raw != "":
        return tuple(c.strip().lower() for c in raw.split(",") if c.strip())
    return ("sports",)


@dataclass(frozen=True)
class InsiderFlowParams:
    name: str = "insider_flow"
    mode: Mode = field(default_factory=_default_mode)

    # ── Signal filters ───────────────────────────────────────────────────────
    # Only copy BUYs ≥ this notional…
    min_cash_size_usdc: float = field(
        default_factory=lambda: float(_env("MIN_CASH", "5000")))
    # …at long odds (insider EV lives below ~0.35; also auto-excludes
    # market-makers and favorites bought at 0.9+)…
    max_entry_odds: float = field(
        default_factory=lambda: float(_env("MAX_ODDS", "0.35")))
    # …from wallets younger than this / with fewer prior trades than this.
    max_wallet_age_days: float = field(
        default_factory=lambda: float(_env("MAX_WALLET_AGE_DAYS", "14")))
    max_prior_trades: int = field(
        default_factory=lambda: int(_env("MAX_PRIOR_TRADES", "10")))
    exclude_title_patterns: tuple = field(default_factory=_default_exclude_titles)
    exclude_categories: tuple = field(default_factory=_default_exclude_categories)

    # ── Time-value gate ──────────────────────────────────────────────────────
    # Capital locked in a far-future market has opportunity cost (~10%/yr in
    # an index fund) and insiders act on *imminent* events — every documented
    # case resolved within days. Skip markets resolving further out than
    # this, and require the win-case return, linearly annualized over the
    # time to resolution, to clear a hurdle: +5% resolving tomorrow is a
    # great trade, +5% locked for a year is strictly worse than the S&P.
    max_days_to_resolution: float = field(
        default_factory=lambda: float(_env("MAX_DAYS_TO_RESOLUTION", "30")))
    min_annualized_return: float = field(
        default_factory=lambda: float(_env("MIN_ANNUAL_RETURN", "1.0")))

    # ── Sizing + cadence ─────────────────────────────────────────────────────
    # Per-trade copy size. Kept small ($2) until paper results validate the
    # detector — raise via INSIDERFLOW_BET_SIZE when the data justifies it.
    bet_size_usdc: float = field(
        default_factory=lambda: float(_env("BET_SIZE", "2")))
    poll_interval_seconds: int = field(
        default_factory=lambda: int(_env("POLL_INTERVAL", "15")))
    firehose_limit: int = field(
        default_factory=lambda: int(_env("FIREHOSE_LIMIT", "100")))
    # Open positions are exited at resolution; sweep every N polls.
    settle_check_every: int = field(
        default_factory=lambda: int(_env("SETTLE_EVERY", "20")))

    # ── Candidate buffer — select the best signals, don't copy them all ─────
    # Passing candidates are held for this window, ranked by conviction
    # score, and only the top N are copied (the rest are real people being
    # randomly dumb, not insiders). 0 disables buffering (copy immediately).
    # Trade-off: waiting costs entry price on fast movers — the slippage
    # gate still rejects anything that drifted > max_slippage meanwhile.
    buffer_window_seconds: float = field(
        default_factory=lambda: float(_env("BUFFER_SECONDS", "900")))
    buffer_top_n: int = field(
        default_factory=lambda: int(_env("BUFFER_TOP_N", "2")))
    # Safety valve: a burst filling the buffer flushes it early.
    buffer_max: int = field(
        default_factory=lambda: int(_env("BUFFER_MAX", "20")))

    # ── Risk caps (AlgoParams protocol — this algo's pool only) ─────────────
    # Sized for the real prod bankroll (~$20 total): $2 per market, at most
    # five concurrent positions (half the bankroll), stop after losing a
    # quarter of it in a day. Scale via env when the bankroll grows.
    max_position_size_usdc: float = field(
        default_factory=lambda: float(_env("MAX_POSITION", "2")))
    max_total_exposure_usdc: float = field(
        default_factory=lambda: float(_env("MAX_EXPOSURE", "10")))
    daily_loss_limit_usdc: float = field(
        default_factory=lambda: float(_env("DAILY_LOSS_LIMIT", "5")))
    min_order_size_usdc: float = field(
        default_factory=lambda: float(_env("MIN_ORDER", "1")))
    # Wider than copy_trade's 0.05 — these signals move fast and skipping on
    # drift is adverse selection against exactly the trades we want.
    max_slippage: float = field(
        default_factory=lambda: float(_env("MAX_SLIPPAGE", "0.10")))

    # ── Order placement / paper ──────────────────────────────────────────────
    order_type: str = field(
        default_factory=lambda: _env("ORDER_TYPE", "market").lower())
    # Mirrors the planned prod bankroll so paper results are observed under
    # the same cash constraint live trading will face.
    paper_starting_balance: float = field(
        default_factory=lambda: float(_env("PAPER_BALANCE", "20")))
    paper_fee_bps: float = field(
        default_factory=lambda: float(_env("PAPER_FEE_BPS", "0")))


PARAMS = InsiderFlowParams()
