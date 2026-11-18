"""Service behaviour when the database fails (no real database needed)."""

from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from backend.config import Settings
from backend.database.enums import Severity
from backend.scanner.processor import WriteBatch
from backend.scanner.providers.base import RawMessage
from backend.scanner.providers.replay import ReplayProvider
from backend.scanner.service import IngestService
from tests.fixtures.pumpportal import create_msg, mint, sig

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class FlakyStore:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.written: list[int] = []

    async def last_event_time(self) -> datetime | None:
        return None

    async def write(self, batch: WriteBatch) -> int:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("database down")
        self.written.append(len(batch.raw_events))
        return len(batch.raw_events)


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str]] = []

    async def send(self, severity: Severity, title: str, body: str = "") -> bool:
        self.sent.append((severity, title))
        return True


def service(store: FlakyStore, alerter: RecordingAlerter) -> IngestService:
    settings = Settings(_env_file=None, database_url=SecretStr("postgresql+asyncpg://u:p@h/d"))
    store_any: Any = store
    return IngestService(settings, ReplayProvider([]), store_any, None, alerter)  # type: ignore[arg-type]


async def test_batches_are_kept_and_retried_after_database_failure() -> None:
    store, alerter = FlakyStore(failures=2), RecordingAlerter()
    svc = service(store, alerter)
    await svc.start()
    svc.processor.handle(RawMessage("pumpportal", T0, create_msg(mint(1), sig(1)), 10))
    await svc.flush()  # fails
    svc.processor.handle(RawMessage("pumpportal", T0, create_msg(mint(2), sig(2)), 10))
    await svc.flush()  # fails again: alert sent once only
    await svc.flush()  # succeeds with both events
    await svc.stop()
    assert store.written == [2]
    assert [s for s, _ in alerter.sent] == [Severity.ERROR, Severity.INFO]


async def test_feed_ready_grace_period_then_requires_data() -> None:
    clock_now = [T0]
    store, alerter = FlakyStore(0), RecordingAlerter()
    settings = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+asyncpg://u:p@h/d"),
        feed_stale_after_seconds=60,
    )
    store_any: Any = store
    svc = IngestService(
        settings,
        ReplayProvider([]),
        store_any,
        None,  # type: ignore[arg-type]
        alerter,
        clock=lambda: clock_now[0],
    )
    await svc.start()
    assert svc.feed_ready()  # start-up grace period
    clock_now[0] = T0.replace(minute=2)
    assert not svc.feed_ready()  # no data after the grace period
    await svc.stop()
