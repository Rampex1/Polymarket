"""
Copy-trade parameter schema.

This file defines *what knobs exist* — names, types, and docs. No defaults:
every profile block states every knob, and this module reads no environment
variables.
"""

from dataclasses import dataclass, field, fields

from bot.domain.mode import Mode


def _doc(default, doc: str):
    return field(default=default, metadata={"doc": doc})


# The vetted cohort, seated by `python -m algorithms.copy_trade.cohort`. Pinned
# here rather than left to the DB so both arms of an A/B provably watch the
# same wallets, and so the cohort behind a stretch of results is recoverable
# from git. A watchlist activated for a given algorithm name still overrides.
COHORT = (
    "0x032eb1bc893940263ad0b01889f262fc232f2a9e",
    "0x0d42d3504ae69136406708fc1cc3117842932eee",
    "0x161a7f666ca49d592848cf415b42f49a84714103",
    "0x2037bb7a0773499b3afc9b4a4c18ff8fb715473d",
    "0x27f738fe203827445690339104aae35b20bc44b0",
    "0x2cc8cc54f50dbb45fe346612821ca93b6e93262b",
    "0x2cd09d387c5ab462b6dc60bef59304f215108e55",
    "0x4d0e9b029700163625c551a0073636f3dc5ec45b",
    "0x5912794596cd3cc2f36605710fc1fcd6e5886f45",
    "0x7bff96579b20fe3530e140d6a3c223c9f2127cd6",
    "0xa2c908ce8b8386bc8c5471120c5a662551332c23",
    "0xa52b785a5510117ac3ae03d75d029f89a36c9480",
    "0xaa9ae1ef7719af8694d6811817c6d0c22aa43b3e",
    "0xb595d09ce5bbc4d39e3b3d04e80c402d2c8d5922",
    "0xb7cd8599d690b62e5a50e9cabacbbf900d086827",
    "0xc72d7dcdb23597d143a83536fb97b1d7db7efc21",
    "0xcbd0f3b660c1c0609ac25919ec0cea828f7edec4",
    "0xcd3675803ac7c8242a83fd6ecdfe3d2239ae0f01",
    "0xd1c769317bd15de7768a70d0214cf0bbcc531d2b",
    "0xe16d3f2a5807999b358affd9445c3a09e45e5e30",
    "0xfcd0eadb24d78e016e88b9e2a7029e349ba6391d",
)


# Named variants — the one place a knob may differ between two blocks of this
# type, keyed by the `name` a profile declares. The values still live in this
# file, so there is still exactly one place to look up what a knob is set to.
#
# For A/B pairs only: change one screen, hold everything else identical. A
# variant that drifts into general tuning defeats the point of the rule.
VARIANTS: dict[str, dict] = {
    # Control arm: the sports screen as shipped.
    "copy_trade_paper": {
        "watchlist_candidate_wallets": COHORT,
    },
    # Treatment arm for the category question. The sports screen was imported
    # as an assumption and has never been tested against our own P&L, while it
    # currently drops ~86% of consensus signals. This arm keeps sports so the
    # two can be compared on realized outcomes instead of on priors.
    "copy_trade_sports_paper": {
        "watchlist_candidate_wallets": COHORT,
        "exclude_categories": (),
    },
}


@dataclass(frozen=True)
class CopyTradeParams:
    name: str = "copy_trade"
    mode: Mode = Mode.PAPER          # fail-safe; the profile block sets it

    # ── Signal source ────────────────────────────────────────────────────────
    target_address: str = _doc("", "Target's proxy wallet (from their Polymarket profile URL). Preferred over username.")
    target_username: str = _doc("", "Target's username — resolved to a wallet via Gamma /profiles at startup.")

    # ── Snapshot-consensus mode ─────────────────────────────────────────────
    # A non-empty ``watchlist_candidate_wallets`` activates the cohort engine
    # instead of the static target fields above. Consensus is read off the
    # cohort's *standing positions* each snapshot, not off a window of entry
    # events, so agreement accumulated days apart still counts.
    # Empty by default so single-target mode stays the base behaviour and the
    # loader still rejects an algorithm with neither a target nor a cohort.
    # The consensus arms get theirs from VARIANTS.
    watchlist_candidate_wallets: tuple = _doc((), "Cohort proxy-wallet addresses. Non-empty activates consensus mode. An activated watchlist for this algorithm's name overrides it.")
    watchlist_size: int = _doc(0, "Rank the cohort down to this many wallets; 0 uses the candidate list as given.")
    watchlist_min_resolved_bets: int = _doc(50, "Minimum resolved bets before a wallet is eligible.")
    watchlist_confidence_z: float = _doc(1.645, "One-sided confidence multiplier used for the edge lower bound.")
    watchlist_min_copyability_score: float = _doc(0.0, "Reject wallets below this historical copyability score (0..1).")

    snapshot_interval_seconds: int = _doc(300, "Seconds between cohort position snapshots — one API call per wallet, and standing positions don't move fast.")
    consensus_min_leaders: int = _doc(3, "Distinct cohort wallets holding the same side before it counts as consensus. Measured over a 21-wallet vetted cohort, 4 never fired and 3 is already rare.")
    consensus_exit_leaders: int = _doc(1, "Close when cohort support falls to this many wallets or fewer. Must sit below consensus_min_leaders or entries and exits fight; 0 disables decay exits.")
    snapshot_min_responders: float = _doc(0.6, "Fraction of the cohort that must return positions before a decay signal is trusted — a Data-API wobble reads as universal abandonment.")
    consensus_min_margin: int = _doc(2, "Support minus opposition. Five on YES against five on NO is disagreement, not a signal. Held one below consensus_min_leaders so a lone dissenter does not veto.")
    consensus_min_conviction: float = _doc(0.0, "Ignore a wallet's vote below this fraction of its own deployed capital. 0 = count every position.")
    consensus_max_price: float = _doc(0.97, "Skip markets already priced as decided — a finished match sits at 1.00 with nothing left to pay out.")
    consensus_min_price: float = _doc(0.05, "Skip markets priced as already lost. A cohort down 98% is holding a fossil, not an opinion.")
    consensus_max_drift: float = _doc(0.25, "Skip if price has run this far past the cohort's cost basis. Their edge is in the entry; past it you are their exit liquidity.")
    exclude_categories: tuple = _doc(("sports",), "Gamma category/tag substrings to skip. Sports are the most efficiently priced markets on the platform.")
    min_hours_to_resolution: float = _doc(24.0, "Skip markets resolving sooner than this — a coin flip with minutes left is not a copyable edge.")
    max_concurrent_positions: int = _doc(8, "Hard cap on simultaneously open markets for consensus mode.")

    # ── Polling ──────────────────────────────────────────────────────────────
    poll_interval_seconds: int = _doc(20, "Seconds between polls of the target's activity feed.")
    min_trade_size_usdc: float = _doc(0.0, "Ignore target trades smaller than this notional. 0 = no filter.")
    settle_check_every: int = _doc(45, "Polls between resolution sweeps of our open positions — safety net for target REDEEMs missed while the bot was offline.")

    # ── Tier sizing — total target position size for our bet ─────────────────
    # Holding < tier1_min          → skip
    # [tier1_min, tier1_max]       → tier1_size
    # (tier1_max, tier2_max]       → tier2_size
    # (tier2_max, +∞)              → tier3_size
    tier1_min: float = _doc(80_000.0, "Target holding below this → skip entirely (the high-conviction floor).")
    tier1_max: float = _doc(150_000.0, "Upper bound of tier 1.")
    tier1_size: float = _doc(1.0, "Our total position size (USDC) for tier-1 holdings.")
    tier2_max: float = _doc(300_000.0, "Upper bound of tier 2.")
    tier2_size: float = _doc(2.0, "Our total position size (USDC) for tier-2 holdings.")
    tier3_size: float = _doc(3.0, "Our total position size (USDC) above tier2_max.")

    # ── Risk caps (this algorithm's pool only — independent of others) ──────
    max_position_size_usdc: float = _doc(3.0, "Max spend per market.")
    max_total_exposure_usdc: float = _doc(12.0, "Max total open exposure for this algorithm.")
    daily_loss_limit_usdc: float = _doc(4.0, "Suspend buys if realized P&L is down this much today.")
    min_order_size_usdc: float = _doc(1.0, "Skip top-ups smaller than this.")
    max_slippage: float = _doc(0.05, "Skip order if price moved more than this fraction from the signal.")

    # ── Order placement ──────────────────────────────────────────────────────
    order_type: str = _doc("market", "'market' (FOK/FAK) or 'limit' (GTC).")

    # ── Notifications ────────────────────────────────────────────────────────
    # Shares insider_flow's channel for now. Both arms post here, and each
    # message carries its algorithm's display_name, so they are separable —
    # but give copy_trade its own webhook before this generates real volume.
    webhook_url: str = _doc(
        "https://discord.com/api/webhooks/1514867567519596594/tPzzQrqH5_0X5oXIV2iQnH5hFiSMe4h0iNyvQslN5xNHhSUn7lfPOB4_KRdLDKJbe99Q",
        "Per-algorithm Discord webhook URL. Required — there is no global fallback.",
    )

    # ── Paper mode ───────────────────────────────────────────────────────────
    paper_starting_balance: float = _doc(10_000.0, "Virtual balance — seeded into the DB on FIRST run only; later edits need scripts/reset_paper_trade_db.py or a manual UPDATE.")
    paper_fee_bps: float = _doc(0.0, "Modeled paper fee, basis points.")

    def __post_init__(self) -> None:
        """Apply this name's variant overrides, if it has any."""
        known = {f.name for f in fields(self)}
        for knob, value in VARIANTS.get(self.name, {}).items():
            # Same fail-fast contract as the profile loader: a typo'd knob is
            # a crash, never a silent no-op that quietly runs the control.
            if knob not in known:
                raise ValueError(
                    f"VARIANTS['{self.name}'] sets unknown knob '{knob}'."
                )
            object.__setattr__(self, knob, value)

    def validate(self) -> None:
        """Boot-time sanity checks — called by the profile loader."""
        if self.watchlist_size < 0:
            raise ValueError("watchlist_size must be zero or positive.")
        if not self.watchlist_candidate_wallets and not (self.target_address or self.target_username):
            raise ValueError(
                "copy_trade needs a target, or watchlist_candidate_wallets "
                "for consensus mode."
            )
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be 'market' or 'limit', got '{self.order_type}'.")
        if not (0 < self.tier1_min <= self.tier1_max <= self.tier2_max):
            raise ValueError("tiers must satisfy 0 < tier1_min <= tier1_max <= tier2_max.")
        if self.snapshot_interval_seconds <= 0:
            raise ValueError("snapshot_interval_seconds must be positive.")
        if self.consensus_min_leaders < 2:
            raise ValueError("consensus requires at least two leaders.")
        if self.consensus_min_margin > self.consensus_min_leaders:
            raise ValueError("consensus_min_margin above consensus_min_leaders can never be satisfied.")
        if self.consensus_exit_leaders >= self.consensus_min_leaders:
            raise ValueError(
                "consensus_exit_leaders must sit below consensus_min_leaders — "
                "without a hysteresis band a position churns open/closed on one "
                "leader trimming."
            )
        if not (0 <= self.snapshot_min_responders <= 1):
            raise ValueError("snapshot_min_responders is a fraction in [0, 1].")
        if not (0 <= self.consensus_min_price < self.consensus_max_price <= 1):
            raise ValueError("prices must satisfy 0 <= consensus_min_price < consensus_max_price <= 1.")
        if self.max_concurrent_positions <= 0:
            raise ValueError("max_concurrent_positions must be positive.")
        if not self.webhook_url:
            raise ValueError(
                "webhook_url is required — there is no global fallback, so an "
                "algorithm without one would trade without ever notifying."
            )
