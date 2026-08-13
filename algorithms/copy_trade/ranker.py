"""Offline-testable wallet ranking for multi-leader copy trading.

The ranker intentionally consumes normalized resolved bets rather than a
specific vendor schema.  A Dune importer can populate that boundary without
putting query details or network calls into the strategy's hot loop.
"""

from dataclasses import dataclass
from math import sqrt
from statistics import stdev
from typing import Iterable, Optional, Protocol

from bot.storage import db


@dataclass(frozen=True)
class ResolvedBet:
    wallet: str
    entry_price: float
    outcome: float
    resolved_at: int
    copyability_score: float = 1.0

    @property
    def edge(self) -> float:
        return self.outcome - self.entry_price


@dataclass(frozen=True)
class WalletScore:
    wallet: str
    sample_size: int
    mean_edge: float
    standard_error: float
    edge_lower_bound: float
    copyability_score: float
    last_resolved_at: int


class ResolvedBetSource(Protocol):
    def resolved_bets(self, wallets: Iterable[str]) -> list[ResolvedBet]: ...


class SQLiteResolvedBetSource:
    """History boundary populated by an offline Dune/CSV ingestion job.

    Keeping ingestion out of the worker prevents a vendor outage or a long
    historical query from delaying leader-event detection.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS wallet_resolved_bets (
        wallet TEXT NOT NULL,
        entry_price REAL NOT NULL,
        outcome REAL NOT NULL,
        resolved_at INTEGER NOT NULL,
        copyability_score REAL NOT NULL DEFAULT 1.0,
        PRIMARY KEY (wallet, resolved_at, entry_price, outcome)
    );
    CREATE INDEX IF NOT EXISTS idx_wallet_resolved_bets_wallet
        ON wallet_resolved_bets(wallet, resolved_at);
    """

    def resolved_bets(self, wallets: Iterable[str]) -> list[ResolvedBet]:
        candidates = tuple(wallet.lower() for wallet in wallets)
        if not candidates:
            return []
        conn = db.get()
        conn.executescript(self._SCHEMA)
        placeholders = ", ".join("?" for _ in candidates)
        rows = conn.execute(
            f"SELECT wallet, entry_price, outcome, resolved_at, copyability_score "
            f"FROM wallet_resolved_bets WHERE lower(wallet) IN ({placeholders})",
            candidates,
        ).fetchall()
        return [ResolvedBet(str(r["wallet"]), float(r["entry_price"]),
                            float(r["outcome"]), int(r["resolved_at"]),
                            float(r["copyability_score"])) for r in rows]


def store_resolved_bets(bets: Iterable[ResolvedBet]) -> int:
    """Write reconstructed history. Returns the number of new rows.

    The natural primary key makes re-ingestion free: rerunning the job over a
    wallet already covered is a no-op rather than a duplicate that would
    silently double-weight that wallet in the ranking.
    """
    rows = [(b.wallet.lower(), b.entry_price, b.outcome, b.resolved_at,
             b.copyability_score) for b in bets]
    if not rows:
        return 0
    conn = db.get()
    conn.executescript(SQLiteResolvedBetSource._SCHEMA)
    with conn:
        before = conn.execute("SELECT COUNT(*) FROM wallet_resolved_bets").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO wallet_resolved_bets "
            "(wallet, entry_price, outcome, resolved_at, copyability_score) "
            "VALUES (?, ?, ?, ?, ?)", rows,
        )
        after = conn.execute("SELECT COUNT(*) FROM wallet_resolved_bets").fetchone()[0]
    return after - before


def known_wallets() -> list[str]:
    """Every wallet we already hold history for — the standing candidate pool."""
    conn = db.get()
    conn.executescript(SQLiteResolvedBetSource._SCHEMA)
    return [str(r["wallet"]) for r in conn.execute(
        "SELECT DISTINCT wallet FROM wallet_resolved_bets ORDER BY wallet")]


def score_wallet(wallet: str, bets: list[ResolvedBet], confidence_z: float) -> WalletScore | None:
    """Compute a conservative confidence-bound score for one wallet."""
    valid = [b for b in bets if 0 < b.entry_price < 1 and b.outcome in (0.0, 1.0)]
    if not valid:
        return None
    edges = [b.edge for b in valid]
    n = len(edges)
    mean = sum(edges) / n
    # One result has no sample variance; assigning infinite uncertainty keeps
    # it from ever passing a positive lower-bound gate.
    se = stdev(edges) / sqrt(n) if n > 1 else float("inf")
    lower = mean - confidence_z * se
    return WalletScore(
        wallet=wallet.lower(), sample_size=n, mean_edge=mean,
        standard_error=se, edge_lower_bound=lower,
        copyability_score=sum(b.copyability_score for b in valid) / n,
        last_resolved_at=max(b.resolved_at for b in valid),
    )


def rank_wallets(
    bets: Iterable[ResolvedBet], min_resolved_bets: int, confidence_z: float,
    min_copyability_score: float,
) -> list[WalletScore]:
    grouped: dict[str, list[ResolvedBet]] = {}
    for bet in bets:
        grouped.setdefault(bet.wallet.lower(), []).append(bet)
    scores = [score_wallet(wallet, rows, confidence_z) for wallet, rows in grouped.items()]
    eligible = [s for s in scores if s and s.sample_size >= min_resolved_bets
                and s.copyability_score >= min_copyability_score]
    return sorted(eligible, key=lambda s: (s.edge_lower_bound, s.copyability_score), reverse=True)


@dataclass(frozen=True)
class HoldoutResult:
    """Did picking wallets on past data predict anything about later data?"""

    split_at: int
    judgeable: int
    selected_edge: float
    rest_edge: float
    rank_correlation: float
    # (wallet, lower bound it was picked on, edge it actually delivered after)
    per_wallet: tuple[tuple[str, float, float], ...]

    @property
    def lift(self) -> float:
        """How much better the picks did than the wallets we passed over.

        This is the number that matters. A high `selected_edge` on its own can
        just mean the whole pool did well in the holdout period; only the gap
        says the *ranking* carried information.
        """
        return self.selected_edge - self.rest_edge


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation. Pearson over ranks, ties broken by position."""
    n = len(xs)
    if n < 3:
        return 0.0
    rank = lambda vs: [i for i, _ in sorted(enumerate(vs), key=lambda p: p[1])]
    rx, ry = [0.0] * n, [0.0] * n
    for pos, idx in enumerate(rank(xs)):
        rx[idx] = pos
    for pos, idx in enumerate(rank(ys)):
        ry[idx] = pos
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def holdout_report(
    bets: Iterable[ResolvedBet], *, split_at: int, min_resolved_bets: int,
    confidence_z: float, min_copyability_score: float, top_n: int,
    min_holdout_bets: int = 20,
) -> Optional[HoldoutResult]:
    """Rank on bets before `split_at`, then score the picks on bets after it.

    The honest test of a wallet selector, and the one `persistence_passes`
    only gestures at: that returns a single pass/fail for the whole pool,
    which a few strong wallets can carry. This asks the question the bot
    actually faces — *if I had chosen a cohort on this date, would those
    wallets have outperformed the ones I passed over?*

    Both groups are restricted to wallets with at least `min_holdout_bets`
    after the split, so the comparison is not between wallets we can measure
    and wallets we cannot. Returns None when the split leaves too little on
    either side to say anything.
    """
    rows = list(bets)
    train = [b for b in rows if b.resolved_at < split_at]
    later: dict[str, list[ResolvedBet]] = {}
    for b in rows:
        if b.resolved_at >= split_at:
            later.setdefault(b.wallet.lower(), []).append(b)

    judgeable = {w for w, rs in later.items() if len(rs) >= min_holdout_bets}
    ranked = [s for s in rank_wallets(train, min_resolved_bets, confidence_z,
                                      min_copyability_score)
              if s.wallet in judgeable]
    if len(ranked) < 2:
        return None

    holdout_edge = {w: sum(b.edge for b in rs) / len(rs) for w, rs in later.items()}
    selected, rest = ranked[:top_n], ranked[top_n:]
    if not selected or not rest:
        return None

    # Equal weight per wallet, not per bet: the question is whether we picked
    # good *wallets*, and pooling would let one hyperactive wallet answer it.
    mean = lambda ss: sum(holdout_edge[s.wallet] for s in ss) / len(ss)
    return HoldoutResult(
        split_at=split_at,
        judgeable=len(ranked),
        selected_edge=mean(selected),
        rest_edge=mean(rest),
        rank_correlation=_spearman([s.edge_lower_bound for s in ranked],
                                   [holdout_edge[s.wallet] for s in ranked]),
        per_wallet=tuple((s.wallet, s.edge_lower_bound, holdout_edge[s.wallet])
                         for s in selected),
    )


def persistence_passes(
    bets: Iterable[ResolvedBet], min_resolved_bets: int, confidence_z: float,
    min_copyability_score: float, top_n: int,
) -> bool:
    """Require the early-period winners to retain positive late-period edge."""
    rows = sorted(bets, key=lambda b: b.resolved_at)
    if len(rows) < 2:
        return False
    split = len(rows) // 2
    early = rank_wallets(rows[:split], min_resolved_bets, confidence_z, min_copyability_score)
    leaders = {s.wallet for s in early[:top_n]}
    if not leaders:
        return False
    late = [b for b in rows[split:] if b.wallet.lower() in leaders]
    if not late:
        return False
    return sum(b.edge for b in late) / len(late) > 0
