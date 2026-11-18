"""Paper positions and exit rules.

Gains are measured on what the remaining tokens would *really* fetch now (executable sale value,
all costs included) against the matching share of the all-in entry cost. Exit decisions are then
executed after the simulated latency, at the price prevailing at that moment: like a real stop,
an exit trigger never guarantees its trigger price.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from backend.trading.config import ExitRules


@dataclass(slots=True)
class PaperPosition:
    strategy: str
    mint: str
    opened_at: datetime
    cost_lamports: int  # SOL in + network fee + rent: everything spent to open
    quantity: int  # raw tokens received
    remaining: int
    entry_price: Decimal
    db_id: int | None = None
    proceeds_lamports: int = 0  # net SOL received from sales, rent refund included
    sold_quantity: int = 0
    tp1_done: bool = False
    tp2_done: bool = False
    peak_gain: Decimal = Decimal(0)
    exit_reason: str | None = None
    selling: bool = False  # a sell order is pending
    trade_seq: int = 0

    def cost_basis_remaining(self) -> Decimal:
        return Decimal(self.cost_lamports) * Decimal(self.remaining) / Decimal(self.quantity)

    def gain(self, remaining_value: int) -> Decimal:
        basis = self.cost_basis_remaining()
        return Decimal(remaining_value) / basis - 1 if basis > 0 else Decimal(0)

    def next_trade_key(self, run_id: str) -> str:
        self.trade_seq += 1
        return f"{run_id}:{self.strategy}:{self.mint}:{self.opened_at.isoformat()}:{self.trade_seq}"


@dataclass(frozen=True, slots=True)
class ExitDecision:
    quantity: int
    reason: str


def _fraction_of(quantity: int, fraction: Decimal, cap: int) -> int:
    return min(cap, max(1, int(Decimal(quantity) * fraction)))


def evaluate_exit(
    position: PaperPosition,
    rules: ExitRules,
    now: datetime,
    remaining_value: int | None,
    safety_reason: str | None = None,
) -> ExitDecision | None:
    """Return the sale to perform now, if any. Safety exits take precedence over everything."""
    if position.selling or position.remaining <= 0:
        return None
    if safety_reason is not None:
        return ExitDecision(position.remaining, safety_reason)
    age = now - position.opened_at
    if remaining_value is None:
        # No executable price (e.g. graduated without aggregator): only time can force an exit.
        if age >= timedelta(minutes=rules.max_hold_minutes):
            return ExitDecision(position.remaining, "max_hold_time")
        return None

    gain = position.gain(remaining_value)
    position.peak_gain = max(position.peak_gain, gain)
    if gain <= -rules.stop_loss:
        return ExitDecision(position.remaining, "stop_loss")
    # Take-profit flags are set by the engine only once the sale is actually filled.
    if not position.tp1_done and gain >= rules.take_profit_1:
        qty = _fraction_of(position.quantity, rules.tp1_fraction, position.remaining)
        return ExitDecision(qty, "take_profit_1")
    if position.tp1_done and not position.tp2_done and gain >= rules.take_profit_2:
        qty = _fraction_of(position.quantity, rules.tp2_fraction, position.remaining)
        return ExitDecision(qty, "take_profit_2")
    if position.tp1_done:
        peak_value = 1 + position.peak_gain
        if (1 + gain) <= peak_value * (1 - rules.trailing_stop):
            return ExitDecision(position.remaining, "trailing_stop")
    if (
        not position.tp1_done
        and age >= timedelta(minutes=rules.stagnation_minutes)
        and gain < rules.stagnation_min_gain
    ):
        return ExitDecision(position.remaining, "stagnation")
    if age >= timedelta(minutes=rules.max_hold_minutes):
        return ExitDecision(position.remaining, "max_hold_time")
    return None
