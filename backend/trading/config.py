"""Paper-trading parameters (strategies, exits, risk limits, execution cost model).

All values are starting hypotheses from docs/TOOLING_AND_STRATEGY.md, versioned by hash so that
every simulated trade can be traced back to the exact parameters that produced it. Amounts are
in lamports (1 SOL = 1e9 lamports); the reference conversion used for the defaults is about
150 EUR/SOL (2 SOL ~ the 300 EUR demonstration capital of the specification).
"""

import hashlib
import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

SOL = 10**9


class ExitRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    take_profit_1: Decimal = Decimal("0.25")
    tp1_fraction: Decimal = Decimal("0.40")  # of the initial quantity
    take_profit_2: Decimal = Decimal("0.50")
    tp2_fraction: Decimal = Decimal("0.30")
    trailing_stop: Decimal = Decimal("0.20")  # from the peak, once TP1 was taken
    stop_loss: Decimal = Decimal("0.15")
    stagnation_minutes: float = 10.0
    stagnation_min_gain: Decimal = Decimal("0.05")
    max_hold_minutes: float = 20.0
    sell_pressure_window_seconds: int = 60
    sell_pressure_ratio: Decimal = Decimal("2.0")  # SOL sold / SOL bought in the window
    sell_pressure_min_lamports: int = 1 * SOL


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    enabled: bool = True
    exits: ExitRules = ExitRules()
    # Strategy A: confirmed momentum on the bonding curve
    entry_bands: tuple[str, ...] = ("candidate", "priority")
    min_curve_progress: Decimal = Decimal("0.25")
    max_curve_progress: Decimal = Decimal("0.70")
    min_unique_buyers: int = 40
    # Strategy B: pullback after graduation (needs executable post-migration quotes)
    min_drawdown_after_migration: Decimal = Decimal("0.30")
    min_rebound_from_low: Decimal = Decimal("0.05")
    max_minutes_after_migration: float = 120.0
    # Strategy C: random control among security-passing analysed tokens
    control_probability: Decimal = Decimal("0.10")


class RiskLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_capital_lamports: int = 2 * SOL
    trading_capital_lamports: int = 1 * SOL
    max_position_lamports: int = 70_000_000
    min_position_lamports: int = 10_000_000
    max_open_positions: int = 2
    daily_loss_fraction: Decimal = Decimal("0.03")  # of total capital
    weekly_loss_fraction: Decimal = Decimal("0.07")
    max_entries_per_day: int = 10
    loss_streak_pause_after: int = 3
    loss_streak_pause_minutes: float = 60.0
    fee_reserve_lamports: int = 50_000_000
    token_cooldown_hours: float = 24.0
    max_entry_price_impact: Decimal = Decimal("0.03")  # fees included


class ExecutionModel(BaseModel):
    """Conservative fill model: latency, fees, extra slippage and simulated failures."""

    model_config = ConfigDict(frozen=True)

    latency_ms: int = 1500
    base_fee_lamports: int = 5_000
    priority_fee_lamports: int = 50_000
    tip_lamports: int = 0
    curve_fee_bps: int = 125
    aggregator_fee_bps: int = 50
    extra_slippage_bps: int = 100
    buy_failure_rate: Decimal = Decimal("0.05")
    sell_failure_rate: Decimal = Decimal("0.03")
    token_account_rent_lamports: int = 2_039_280

    @property
    def network_fee_lamports(self) -> int:
        return self.base_fee_lamports + self.priority_fee_lamports + self.tip_lamports


def default_strategies() -> tuple[StrategyConfig, ...]:
    return (StrategyConfig(name="A"), StrategyConfig(name="B"), StrategyConfig(name="C"))


class PaperTradingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = "paper-v1"
    strategies: tuple[StrategyConfig, ...] = Field(default_factory=default_strategies)
    risk: RiskLimits = RiskLimits()
    execution: ExecutionModel = ExecutionModel()
    seed: int = 20261118
    snapshot_interval_minutes: float = 5.0

    def strategy(self, name: str) -> StrategyConfig:
        return next(s for s in self.strategies if s.name == name)

    def version_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()
