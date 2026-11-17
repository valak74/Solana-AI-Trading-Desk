"""Recording of operational events (start, stop, feed outages, limit hits...)."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import Severity
from backend.database.models import SystemEvent
from backend.database.session import transaction
from backend.monitoring.logging import get_logger

log = get_logger(__name__)


async def record_system_event(
    factory: async_sessionmaker[AsyncSession],
    event_type: str,
    severity: Severity,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Persist an event; logs and returns False instead of raising if the database is down."""
    try:
        async with transaction(factory) as session:
            session.add(
                SystemEvent(event_type=event_type, severity=severity, payload=payload or {})
            )
    except Exception as exc:
        log.error("system_event_not_recorded", event_type=event_type, error_type=type(exc).__name__)
        return False
    return True
