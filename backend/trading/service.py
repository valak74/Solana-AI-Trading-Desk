"""Live wiring of the paper-trading engine: event hooks, periodic ticks, kill switch cache.

All engine calls go through one lock: the read loop, the analysis loop and the tick loop run
concurrently, and the engine's state must only be changed by one of them at a time.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.analysis.engine import AnalysisResult, AnalysisTarget
from backend.monitoring.logging import get_logger
from backend.risk.kill_switch import is_active
from backend.scanner.providers.base import ProviderItem
from backend.trading.engine import PaperTradingEngine
from backend.trading.strategies import AnalysisSignal

log = get_logger(__name__)


def signal_from(target: AnalysisTarget, result: AnalysisResult) -> AnalysisSignal:
    return AnalysisSignal(
        token_id=target.token_id,
        mint=target.mint,
        at=result.analyzed_at,
        decision=result.decision,
        critical_flags=tuple(result.findings.critical),
        total_score=result.final.total,
        metrics=dict(result.findings.metrics),
    )


class PaperTradingService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        tick_interval: float = 2.0,
        kill_switch_refresh: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._factory = factory
        self._tick_interval = tick_interval
        self._kill_refresh = kill_switch_refresh
        self._clock = clock
        self._lock = asyncio.Lock()
        self._kill_active = True  # until read: in doubt, no new position
        self._engine: PaperTradingEngine | None = None
        self.errors = 0

    def attach(self, engine: PaperTradingEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> PaperTradingEngine:
        if self._engine is None:
            raise RuntimeError("no engine attached")
        return self._engine

    def kill_switch_active(self) -> bool:
        return self._kill_active

    def pinned_mints(self) -> set[str]:
        return self.engine.pinned_mints()

    async def start(self) -> None:
        self._kill_active = await is_active(self._factory)
        async with self._lock:
            await self.engine.start(self._clock())

    async def on_raw(self, item: ProviderItem) -> None:
        async with self._lock:
            await self._guard(self.engine.on_raw(item))

    async def on_analysis(self, target: AnalysisTarget, result: AnalysisResult) -> None:
        async with self._lock:
            await self._guard(self.engine.on_analysis(signal_from(target, result)))

    async def _guard(self, call: Awaitable[None]) -> None:
        try:
            await call
        except Exception as exc:
            self.errors += 1
            log.error("paper_engine_error", error_type=type(exc).__name__)

    async def run(self) -> None:
        elapsed = 0.0
        while True:
            await asyncio.sleep(self._tick_interval)
            elapsed += self._tick_interval
            if elapsed >= self._kill_refresh:
                elapsed = 0.0
                self._kill_active = await is_active(self._factory)
            async with self._lock:
                await self._guard(self.engine.tick(self._clock()))
