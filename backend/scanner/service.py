"""Ingestion service: provider -> processor -> batched writes, plus maintenance and alerts.

Everything runs in one asyncio process. The reader never waits on the database: rows are
accumulated in memory and flushed every second. If the database is unavailable, batches are
kept and retried (bounded memory); overflow is counted and reported, never silently lost.
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.enums import Severity
from backend.database.system_events import record_system_event
from backend.monitoring.alerts import Alerter
from backend.monitoring.logging import get_logger
from backend.scanner.feed_monitor import FeedMonitor, GapClosed, GapOpened
from backend.scanner.processor import IngestProcessor, WriteBatch
from backend.scanner.providers.base import MarketDataProvider
from backend.scanner.store import IngestStore
from backend.scanner.tracking import TrackingRegistry
from backend.scanner.verifier import SampleVerifier

log = get_logger(__name__)

MAX_PENDING_RAW_EVENTS = 200_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def build_processor(settings: Settings, last_seen: datetime | None) -> IngestProcessor:
    registry = TrackingRegistry(
        window=timedelta(minutes=settings.track_window_minutes),
        migrated_window=timedelta(minutes=settings.track_migrated_window_minutes),
        min_trades=settings.track_min_trades,
        min_trades_window=timedelta(minutes=settings.track_min_trades_window_minutes),
        max_tracked=settings.max_tracked_tokens,
    )
    monitor = FeedMonitor(timedelta(seconds=settings.feed_stale_after_seconds), last_seen)
    return IngestProcessor(
        registry,
        monitor,
        tracked_pools=settings.tracked_pools,
        snapshot_min_interval=timedelta(seconds=settings.snapshot_min_interval_seconds),
        verify_sample_rate=settings.verify_sample_rate,
    )


def _format_minutes(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 60:.1f} min"


class IngestService:
    def __init__(
        self,
        settings: Settings,
        provider: MarketDataProvider,
        store: IngestStore,
        factory: async_sessionmaker[AsyncSession],
        alerter: Alerter,
        verifier: SampleVerifier | None = None,
        *,
        flush_interval: float = 1.0,
        tick_interval: float = 5.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._store = store
        self._factory = factory
        self._alerter = alerter
        self._verifier = verifier
        self._flush_interval = flush_interval
        self._tick_interval = tick_interval
        self._clock = clock
        self._stale_after = timedelta(seconds=settings.feed_stale_after_seconds)
        self._processor: IngestProcessor | None = None
        self._pending = WriteBatch()
        self._dropped_raw_events = 0
        self._db_failing = False
        self._started_at = clock()
        self._tasks: list[asyncio.Task[Any]] = []

    @property
    def processor(self) -> IngestProcessor:
        if self._processor is None:
            raise RuntimeError("ingest service not started")
        return self._processor

    async def start(self) -> None:
        last_seen = await self._store.last_event_time()
        self._processor = build_processor(self._settings, last_seen)
        self._started_at = self._clock()
        loops = [self._read_loop(), self._flush_loop(), self._tick_loop(), self._stats_loop()]
        if self._verifier is not None:
            loops.append(self._verifier.run())
        self._tasks = [asyncio.create_task(loop) for loop in loops]

    async def stop(self) -> None:
        await self._provider.close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._processor is not None:
            await self.flush()  # best effort: persist what was already received

    def feed_ready(self) -> bool:
        """Fresh data, with a start-up grace period before the first message."""
        now = self._clock()
        if self._processor is None:
            return False
        monitor = self._processor.monitor
        if monitor.is_fresh(now):
            return True
        received_since_start = (
            monitor.last_message_at is not None and monitor.last_message_at >= self._started_at
        )
        return not received_since_start and now - self._started_at <= self._stale_after

    # --- loops -----------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        async for item in self._provider.items():
            result = self.processor.handle(item)
            if result.track:
                await self._provider.track(result.track)
            if result.verify is not None and self._verifier is not None:
                self._verifier.submit(result.verify)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    async def flush(self) -> None:
        batch = self.processor.take_batch()
        self._pending.merge(batch)
        if len(self._pending) == 0:
            return
        await self._alert_gaps(batch)
        try:
            await self._store.write(self._pending)
        except Exception as exc:
            await self._on_write_failure(exc)
            return
        if self._db_failing:
            self._db_failing = False
            await self._alerter.send(Severity.INFO, "Base de données de nouveau accessible")
        self._pending = WriteBatch()

    async def _on_write_failure(self, exc: Exception) -> None:
        log.error(
            "ingest_write_failed",
            error_type=type(exc).__name__,
            pending=len(self._pending.raw_events),
        )
        overflow = len(self._pending.raw_events) - MAX_PENDING_RAW_EVENTS
        if overflow > 0:
            del self._pending.raw_events[:overflow]
            self._dropped_raw_events += overflow
        if not self._db_failing:
            self._db_failing = True
            await self._alerter.send(
                Severity.ERROR,
                "Écriture en base impossible",
                "Les données sont gardées en mémoire et réessayées.",
            )

    async def _alert_gaps(self, batch: WriteBatch) -> None:
        for source, gap in batch.gaps:
            if isinstance(gap, GapOpened) and gap.reason != "service_down":
                await self._alerter.send(
                    Severity.WARNING,
                    f"Flux {source} interrompu",
                    f"Cause : {gap.reason}. Aucun achat ne doit être décidé pendant la coupure.",
                )
            elif isinstance(gap, GapClosed):
                await self._alerter.send(
                    Severity.INFO,
                    f"Flux {source} rétabli",
                    f"Données manquantes : {_format_minutes(gap.ended_at - gap.started_at)} "
                    f"(cause : {gap.reason}).",
                )

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval)
            expired = self.processor.tick(self._clock(), self._provider.name)
            if expired:
                await self._provider.untrack(expired)

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.ingest_stats_interval_seconds)
            await self.record_stats()

    async def record_stats(self) -> None:
        stats = self.processor.take_stats()
        payload = stats.as_payload() | {
            "interval_seconds": self._settings.ingest_stats_interval_seconds,
            "tracked_tokens": len(self.processor.registry),
            "rejected_capacity": self.processor.registry.rejected_capacity,
            "dropped_raw_events": self._dropped_raw_events,
            "verification_dropped": self._verifier.dropped if self._verifier else 0,
        }
        with contextlib.suppress(Exception):
            payload["raw_events_table_bytes"] = await self._store.raw_events_size_bytes()
        await record_system_event(self._factory, "ingest_stats", Severity.INFO, payload)
        log.info("ingest_stats", **payload)
