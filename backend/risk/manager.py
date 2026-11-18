"""Deterministic risk manager with an absolute veto.

Each strategy has its own virtual portfolio under identical limits, so strategies can be compared
fairly. A position is budgeted as a possible TOTAL loss (a stop does not protect against a rug),
so the size of a new position never exceeds what remains of the daily and weekly loss budgets
once every open position is assumed lost.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from backend.trading.config import RiskLimits


@dataclass(slots=True)
class StrategyBook:
    """Virtual portfolio of one strategy (lamports)."""

    cash: int
    open_cost: dict[str, int] = field(default_factory=dict)  # mint -> all-in cost still at risk
    realized: list[tuple[datetime, int]] = field(default_factory=list)  # (closed_at, pnl)
    realized_total: int = 0  # all-time realised PnL
    entries: list[datetime] = field(default_factory=list)
    last_entry_by_token: dict[str, datetime] = field(default_factory=dict)
    loss_streak: int = 0
    paused_until: datetime | None = None
    peak_equity: int = 0

    def record_close(self, at: datetime, pnl: int, limits: RiskLimits) -> None:
        self.realized.append((at, pnl))
        self.loss_streak = self.loss_streak + 1 if pnl < 0 else 0
        if self.loss_streak >= limits.loss_streak_pause_after:
            self.paused_until = at + timedelta(minutes=limits.loss_streak_pause_minutes)
            self.loss_streak = 0

    def realized_loss_since(self, since: datetime) -> int:
        """Net realised loss (positive number) since ``since``; gains offset losses."""
        net = sum(pnl for t, pnl in self.realized if t >= since)
        return max(0, -net)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    size_lamports: int = 0
    reason: str = "approved"


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(now: datetime) -> datetime:
    return _day_start(now) - timedelta(days=now.weekday())


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        kill_switch_active: Callable[[], bool],
        feed_healthy: Callable[[datetime], bool],
    ) -> None:
        self.limits = limits
        self._kill_switch_active = kill_switch_active
        self._feed_healthy = feed_healthy

    def new_book(self) -> StrategyBook:
        cash = self.limits.trading_capital_lamports
        return StrategyBook(cash=cash, peak_equity=cash)

    def check_entry(
        self, book: StrategyBook, mint: str, now: datetime, entry_impact: Decimal | None
    ) -> RiskDecision:
        lim = self.limits
        if self._kill_switch_active():
            return RiskDecision(False, reason="kill_switch_active")
        if not self._feed_healthy(now):
            return RiskDecision(False, reason="feed_unhealthy")
        if book.paused_until is not None and now < book.paused_until:
            return RiskDecision(False, reason="loss_streak_pause")
        if mint in book.open_cost:
            return RiskDecision(False, reason="already_in_position")
        last = book.last_entry_by_token.get(mint)
        if last is not None and now - last < timedelta(hours=lim.token_cooldown_hours):
            return RiskDecision(False, reason="token_cooldown")
        if len(book.open_cost) >= lim.max_open_positions:
            return RiskDecision(False, reason="max_open_positions")
        if sum(1 for t in book.entries if t >= _day_start(now)) >= lim.max_entries_per_day:
            return RiskDecision(False, reason="max_entries_per_day")
        if entry_impact is None:
            return RiskDecision(False, reason="no_cost_estimate")
        if entry_impact > lim.max_entry_price_impact:
            return RiskDecision(False, reason="price_impact_too_high")

        at_risk = sum(book.open_cost.values())
        daily_limit = int(lim.total_capital_lamports * lim.daily_loss_fraction)
        weekly_limit = int(lim.total_capital_lamports * lim.weekly_loss_fraction)
        daily_room = daily_limit - book.realized_loss_since(_day_start(now)) - at_risk
        weekly_room = weekly_limit - book.realized_loss_since(_week_start(now)) - at_risk
        cash_room = book.cash - lim.fee_reserve_lamports
        size = min(lim.max_position_lamports, daily_room, weekly_room, cash_room)
        if size < lim.min_position_lamports:
            reason = (
                "daily_loss_budget_exhausted"
                if daily_room < lim.min_position_lamports
                else "weekly_loss_budget_exhausted"
                if weekly_room < lim.min_position_lamports
                else "insufficient_cash"
            )
            return RiskDecision(False, reason=reason)
        return RiskDecision(True, size_lamports=size)
