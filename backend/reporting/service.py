"""Building the report from the database, and the weekly Telegram summary."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import Severity
from backend.database.models import SystemEvent
from backend.database.system_events import record_system_event
from backend.monitoring.alerts import Alerter
from backend.monitoring.logging import get_logger
from backend.reporting.dataset import (
    load_gaps,
    load_outcome_labels,
    load_positions,
    measurement_start,
)
from backend.reporting.render import render_short
from backend.reporting.report import Report, ReportConfig, build_report

log = get_logger(__name__)

WEEKLY_EVENT = "weekly_report_sent"


async def generate_report(
    factory: async_sessionmaker[AsyncSession],
    config: ReportConfig,
    *,
    run_id: str = "live",
    start: datetime | None = None,
    end: datetime | None = None,
) -> Report | None:
    end = end or datetime.now(UTC)
    start = start or await measurement_start(factory)
    if start is None or start >= end:
        return None
    positions = await load_positions(factory, run_id, start, end)
    gaps = await load_gaps(factory, start, end)
    labels = await load_outcome_labels(factory, start, end)
    return build_report(
        positions,
        gaps,
        labels,
        run_id=run_id,
        start=start,
        end=end,
        generated_at=datetime.now(UTC),
        config=config,
    )


class WeeklySummary:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        alerter: Alerter,
        config: ReportConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        every: timedelta = timedelta(days=7),
    ) -> None:
        self._factory = factory
        self._alerter = alerter
        self._config = config
        self._clock = clock
        self._every = every

    async def _last_sent(self) -> datetime | None:
        async with self._factory() as session:
            last: datetime | None = (
                await session.execute(
                    select(SystemEvent.created_at)
                    .where(SystemEvent.event_type == WEEKLY_EVENT)
                    .order_by(SystemEvent.created_at.desc())
                    .limit(1)
                )
            ).scalar()
        return last

    async def run_once(self) -> bool:
        now = self._clock()
        last = await self._last_sent()
        started = await measurement_start(self._factory)
        reference = last or started
        if reference is None or now - reference < self._every:
            return False
        report = await generate_report(self._factory, self._config, end=now)
        if report is None:
            return False
        await self._alerter.send(Severity.INFO, "Rapport hebdomadaire", render_short(report))
        await record_system_event(
            self._factory,
            WEEKLY_EVENT,
            Severity.INFO,
            {"verdict": report.verdict, "days": str(report.days)},
        )
        return True

    async def run(self, interval: float = 3600.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                log.error("weekly_summary_failed", error_type=type(exc).__name__)
            await asyncio.sleep(interval)
