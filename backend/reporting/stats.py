"""Performance statistics on realised paper results. Pure functions, exact integer inputs.

Conventions:
- an *attempt* is any closed position with a known PnL, failed entries included (their PnL is
  the network fee they cost): expectancy, profit factor and drawdown use attempts, so every cost
  of the strategy is counted;
- a *filled trade* is an attempt whose entry was actually filled: win rate and average win/loss
  use filled trades only.
Confidence intervals come from a seeded bootstrap of the mean (reproducible).
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

Q = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class Interval:
    low: Decimal
    high: Decimal


def mean(values: Sequence[int | Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(sum(values, Decimal(0))) / Decimal(len(values))


def bootstrap_mean(
    values: Sequence[int | Decimal],
    *,
    resamples: int = 2000,
    confidence: Decimal = Decimal("0.95"),
    seed: int = 7,
) -> Interval | None:
    """Percentile bootstrap interval of the mean."""
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)  # noqa: S311 - statistics, not cryptography
    data = [Decimal(v) for v in values]
    means = sorted(sum(rng.choices(data, k=n), Decimal(0)) / n for _ in range(resamples))
    tail = (1 - confidence) / 2
    low_index = int(tail * resamples)
    high_index = min(resamples - 1, int((1 - tail) * resamples) - 1)
    return Interval(means[low_index], means[high_index])


def bootstrap_mean_difference(
    a: Sequence[int | Decimal],
    b: Sequence[int | Decimal],
    *,
    resamples: int = 2000,
    confidence: Decimal = Decimal("0.95"),
    seed: int = 11,
) -> Interval | None:
    """Interval of mean(a) - mean(b), resampling both groups independently."""
    if len(a) < 2 or len(b) < 2:
        return None
    rng = random.Random(seed)  # noqa: S311 - statistics, not cryptography
    da, db = [Decimal(v) for v in a], [Decimal(v) for v in b]
    diffs = sorted(
        sum(rng.choices(da, k=len(da)), Decimal(0)) / len(da)
        - sum(rng.choices(db, k=len(db)), Decimal(0)) / len(db)
        for _ in range(resamples)
    )
    tail = (1 - confidence) / 2
    return Interval(
        diffs[int(tail * resamples)], diffs[min(resamples - 1, int((1 - tail) * resamples) - 1)]
    )


def profit_factor(pnls: Sequence[int]) -> Decimal | None:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return None if gains == 0 else Decimal("Infinity")
    return (Decimal(gains) / Decimal(losses)).quantize(Q, ROUND_HALF_EVEN)


def max_drawdown(pnls_in_order: Sequence[int], starting_equity: int) -> Decimal:
    """Largest peak-to-trough fall of equity (fraction of the peak), from realised results."""
    equity = peak = starting_equity
    worst = Decimal(0)
    for pnl in pnls_in_order:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, Decimal(peak - equity) / Decimal(peak))
    return worst.quantize(Q, ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    attempts: int
    filled: int
    failed_entries: int
    total_pnl: int
    expectancy: Decimal | None  # lamports per attempt
    expectancy_ci: Interval | None
    return_per_attempt: Decimal | None  # mean of pnl / cost
    win_rate: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal


def summarize(
    pnls: Sequence[int],
    costs: Sequence[int],
    filled_flags: Sequence[bool],
    starting_equity: int,
    *,
    resamples: int = 2000,
    confidence: Decimal = Decimal("0.95"),
    seed: int = 7,
) -> PerformanceSummary:
    """``pnls``/``costs``/``filled_flags`` are aligned and in chronological order of closing."""
    filled = [p for p, f in zip(pnls, filled_flags, strict=True) if f]
    wins = [p for p in filled if p > 0]
    losses = [p for p in filled if p <= 0]
    returns = [Decimal(p) / Decimal(c) for p, c in zip(pnls, costs, strict=True) if c > 0]
    expectancy = mean(pnls)
    return PerformanceSummary(
        attempts=len(pnls),
        filled=len(filled),
        failed_entries=len(pnls) - len(filled),
        total_pnl=sum(pnls),
        expectancy=None if expectancy is None else expectancy.quantize(Q),
        expectancy_ci=bootstrap_mean(pnls, resamples=resamples, confidence=confidence, seed=seed),
        return_per_attempt=None if not returns else mean(returns).quantize(Q),  # type: ignore[union-attr]
        win_rate=None if not filled else (Decimal(len(wins)) / len(filled)).quantize(Q),
        avg_win=None if not wins else mean(wins).quantize(Q),  # type: ignore[union-attr]
        avg_loss=None if not losses else mean(losses).quantize(Q),  # type: ignore[union-attr]
        profit_factor=profit_factor(pnls),
        max_drawdown=max_drawdown(pnls, starting_equity),
    )
