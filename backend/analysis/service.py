"""Periodic analysis of tracked tokens, within the daily RPC credit budget."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.analysis.engine import AnalysisResult, AnalysisTarget, TokenAnalyzer
from backend.analysis.flows import compute_trade_flow, load_trades
from backend.analysis.repository import AnalysisRepository
from backend.database.enums import Severity
from backend.database.system_events import record_system_event
from backend.integrations.helius import BudgetExceeded, CreditBudget
from backend.monitoring.alerts import Alerter
from backend.monitoring.logging import get_logger
from backend.security.config import AnalysisConfig

log = get_logger(__name__)

# Credits one analysis may need in the worst case; below this we stop for the day.
CREDITS_RESERVE_PER_ANALYSIS = 25


class AnalysisService:
    def __init__(
        self,
        analyzer: TokenAnalyzer,
        repository: AnalysisRepository,
        factory: async_sessionmaker[AsyncSession],
        config: AnalysisConfig,
        budget: CreditBudget,
        alerter: Alerter,
        *,
        interval: float = 20.0,
        min_age: timedelta = timedelta(minutes=3),
        max_age: timedelta = timedelta(minutes=30),
        reanalyze_after: timedelta = timedelta(minutes=10),
        min_trades: int = 15,
        batch: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_result: Callable[[AnalysisTarget, AnalysisResult], Awaitable[None]] | None = None,
    ) -> None:
        self.on_result = on_result
        self._analyzer = analyzer
        self._repo = repository
        self._factory = factory
        self._config = config
        self._budget = budget
        self._alerter = alerter
        self._interval = interval
        self._min_age, self._max_age = min_age, max_age
        self._reanalyze_after = reanalyze_after
        self._min_trades = min_trades
        self._batch = batch
        self._clock = clock
        self._config_id: int | None = None
        self._budget_alerted_on: str | None = None
        self.analyzed = 0

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # never let one bad token stop the loop
                log.error("analysis_cycle_failed", error_type=type(exc).__name__)
            await asyncio.sleep(self._interval)

    async def run_once(self) -> int:
        if self._config_id is None:
            self._config_id = await self._repo.config_version_id(self._config)
        if self._budget.remaining() < CREDITS_RESERVE_PER_ANALYSIS:
            await self._on_budget_exhausted()
            return 0
        now = self._clock()
        targets = await self._repo.candidates(
            now,
            self._min_age,
            self._max_age,
            self._reanalyze_after,
            self._min_trades,
            self._batch,
        )
        done = 0
        for target in targets:
            if self._budget.remaining() < CREDITS_RESERVE_PER_ANALYSIS:
                await self._on_budget_exhausted()
                break
            trades = await load_trades(self._factory, target.mint, now)
            flow = compute_trade_flow(
                trades,
                now,
                timedelta(seconds=self._config.bounds.recent_window_seconds),
                timedelta(seconds=self._config.bounds.previous_window_seconds),
            )
            launches = await self._repo.creator_launches(target.creator, now)
            started = time.monotonic()
            try:
                result = await self._analyzer.analyze(target, flow, launches)
            except BudgetExceeded:
                await self._on_budget_exhausted()
                break
            duration_ms = int((time.monotonic() - started) * 1000)
            await self._repo.save(target, result, self._config_id, duration_ms)
            done += 1
            if self.on_result is not None:
                try:
                    await self.on_result(target, result)
                except Exception as exc:  # the strategist must not stop the analysis loop
                    log.error("analysis_listener_failed", error_type=type(exc).__name__)
        self.analyzed += done
        return done

    async def _on_budget_exhausted(self) -> None:
        day = self._clock().date().isoformat()
        if self._budget_alerted_on == day:
            return
        self._budget_alerted_on = day
        payload = {
            "daily_limit": self._budget.daily_limit,
            "spent": self._budget.spent,
            "calls": dict(self._budget.calls),
        }
        await record_system_event(
            self._factory, "analysis_budget_exhausted", Severity.WARNING, payload
        )
        await self._alerter.send(
            Severity.WARNING,
            "Budget de crédits Helius atteint",
            "Analyses suspendues jusqu'à minuit UTC. L'enregistrement continue.",
        )
