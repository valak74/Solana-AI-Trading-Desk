"""External heartbeat: pings a healthchecks.io-style URL only while the service is ready.

If the process dies, hangs, or loses its database, the pings stop and the external monitor
raises the alarm. This covers the failure mode an in-process alerter cannot report: its own death.
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from backend.monitoring.logging import get_logger

log = get_logger(__name__)

ReadinessCheck = Callable[[], Awaitable[bool]]


class Heartbeat:
    def __init__(self, client: httpx.AsyncClient, ping_url: str, is_ready: ReadinessCheck) -> None:
        self._client = client
        self._ping_url = ping_url
        self._is_ready = is_ready

    async def beat_once(self) -> bool:
        """Ping once if ready. Returns True when a ping was successfully sent."""
        try:
            ready = await self._is_ready()
        except Exception as exc:
            log.warning("heartbeat_readiness_error", error_type=type(exc).__name__)
            ready = False
        if not ready:
            log.warning("heartbeat_skipped", reason="not_ready")
            return False
        try:
            response = await self._client.get(self._ping_url)
        except httpx.HTTPError as exc:
            log.warning("heartbeat_failed", error_type=type(exc).__name__)
            return False
        if response.is_success:
            return True
        log.warning("heartbeat_failed", status=response.status_code)
        return False

    async def run(self, interval_seconds: float) -> None:
        while True:
            await self.beat_once()
            await asyncio.sleep(interval_seconds)
