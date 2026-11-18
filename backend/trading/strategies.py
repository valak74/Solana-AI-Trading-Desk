"""Entry signals of the three paper strategies. Pure functions: no I/O, no randomness inside.

A - confirmed momentum on the bonding curve (from the M3 analysis);
B - pullback after graduation (trade-implied prices for the signal, executable quotes for fills);
C - random control among analysed tokens without critical security flag.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from backend.trading.config import StrategyConfig
from backend.trading.market import TokenMarket


@dataclass(frozen=True, slots=True)
class AnalysisSignal:
    """What the strategist needs from one M3 analysis."""

    token_id: int
    mint: str
    at: datetime
    decision: str  # reject / watch / candidate / priority
    critical_flags: tuple[str, ...]
    total_score: Decimal
    metrics: dict[str, Any] = field(default_factory=dict)

    def metric_decimal(self, key: str) -> Decimal | None:
        value = self.metrics.get(key)
        try:
            return None if value is None else Decimal(str(value))
        except ArithmeticError:
            return None

    @property
    def entry_impact(self) -> Decimal | None:
        return self.metric_decimal("entry_price_impact")


@dataclass(frozen=True, slots=True)
class EntrySignal:
    strategy: str
    reason: str


def strategy_a(cfg: StrategyConfig, signal: AnalysisSignal) -> EntrySignal | None:
    if signal.critical_flags or signal.decision not in cfg.entry_bands:
        return None
    progress = signal.metric_decimal("curve_tokens_sold_share")
    buyers = signal.metric_decimal("unique_buyers")
    if progress is None or not cfg.min_curve_progress <= progress <= cfg.max_curve_progress:
        return None
    if buyers is None or buyers < cfg.min_unique_buyers:
        return None
    return EntrySignal(cfg.name, f"band={signal.decision} progress={progress} buyers={buyers}")


def strategy_c(
    cfg: StrategyConfig, signal: AnalysisSignal, rng: random.Random
) -> EntrySignal | None:
    if signal.critical_flags:
        return None
    if rng.random() >= float(cfg.control_probability):
        return None
    return EntrySignal(cfg.name, "random_control")


def strategy_b(
    cfg: StrategyConfig,
    market: TokenMarket,
    last_analysis: AnalysisSignal | None,
    now: datetime,
) -> EntrySignal | None:
    """Enter after the post-migration dump, once the price rebounds from its low with buyers."""
    if market.migrated_at is None or last_analysis is None or last_analysis.critical_flags:
        return None
    if now - market.migrated_at > timedelta(minutes=cfg.max_minutes_after_migration):
        return None
    prices = [p for t, p in market.implied_prices if t >= market.migrated_at]
    if len(prices) < 10:
        return None
    high = max(prices)
    low_index = min(range(len(prices)), key=prices.__getitem__)
    low, last = prices[low_index], prices[-1]
    if high <= 0 or low_index == len(prices) - 1:
        return None
    drawdown = 1 - low / high
    rebound = last / low - 1
    bought, sold = market.flow(now, timedelta(minutes=2))
    if (
        drawdown >= cfg.min_drawdown_after_migration
        and rebound >= cfg.min_rebound_from_low
        and (bought > sold)
    ):
        return EntrySignal(cfg.name, f"drawdown={drawdown:.3f} rebound={rebound:.3f}")
    return None
