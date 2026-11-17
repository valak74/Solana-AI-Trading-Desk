"""Application assembly: one process running the API and background tasks (measurement stage)."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from backend.api.health import router as health_router
from backend.config import Settings, get_settings
from backend.database.enums import Severity
from backend.database.session import create_engine, create_session_factory
from backend.database.system_events import record_system_event
from backend.monitoring.alerts import Alerter, LogOnlyAlerter, TelegramAlerter
from backend.monitoring.heartbeat import Heartbeat
from backend.monitoring.logging import configure_logging, get_logger
from backend.monitoring.readiness import ReadinessProbe, database_checks, expected_schema_head

log = get_logger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0


def build_alerter(settings: Settings, client: httpx.AsyncClient) -> Alerter:
    if settings.telegram_bot_token is not None and settings.telegram_chat_id is not None:
        return TelegramAlerter(
            client, settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
        )
    return LogOnlyAlerter()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url.get_secret_value(), settings.db_pool_size)
        factory = create_session_factory(engine)
        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        alerter = build_alerter(settings, client)
        probe = ReadinessProbe(database_checks(engine, expected_schema_head()))
        app.state.readiness = probe

        tasks: list[asyncio.Task[None]] = []
        if settings.healthchecks_ping_url is not None:
            heartbeat = Heartbeat(
                client, settings.healthchecks_ping_url.get_secret_value(), probe.is_ready
            )
            tasks.append(asyncio.create_task(heartbeat.run(settings.heartbeat_interval_seconds)))

        payload = {"execution_mode": settings.execution_mode.value, "env": settings.app_env}
        await record_system_event(factory, "service_started", Severity.INFO, payload)
        await alerter.send(
            Severity.INFO, "Trading desk démarré", f"mode={payload['execution_mode']}"
        )
        log.info("service_started", **payload)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await record_system_event(factory, "service_stopped", Severity.INFO, payload)
            await alerter.send(Severity.WARNING, "Trading desk arrêté")
            log.info("service_stopped")
            await client.aclose()
            await engine.dispose()

    app = FastAPI(title="Solana AI Trading Desk", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app
