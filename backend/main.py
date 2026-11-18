"""Application assembly: one process running the API and background tasks (measurement stage)."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.health import router as health_router
from backend.config import Settings, get_settings
from backend.database.enums import Severity
from backend.database.session import create_engine, create_session_factory
from backend.database.system_events import record_system_event
from backend.integrations.helius import HeliusRpc
from backend.monitoring.alerts import Alerter, LogOnlyAlerter, TelegramAlerter
from backend.monitoring.heartbeat import Heartbeat
from backend.monitoring.logging import configure_logging, get_logger
from backend.monitoring.readiness import ReadinessProbe, database_checks, expected_schema_head
from backend.scanner.providers.pumpportal import PumpPortalProvider
from backend.scanner.service import IngestService
from backend.scanner.store import IngestStore
from backend.scanner.verifier import SampleVerifier

log = get_logger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0


def build_alerter(settings: Settings, client: httpx.AsyncClient) -> Alerter:
    if settings.telegram_bot_token is not None and settings.telegram_chat_id is not None:
        return TelegramAlerter(
            client, settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
        )
    return LogOnlyAlerter()


def build_ingest(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    alerter: Alerter,
) -> IngestService:
    key = settings.pumpportal_api_key
    provider = PumpPortalProvider(
        settings.pumpportal_ws_url, key.get_secret_value() if key else None
    )
    verifier = None
    if settings.helius_api_key is not None and settings.verify_sample_rate > 0:
        rpc = HeliusRpc(client, settings.helius_rpc_url, settings.helius_api_key.get_secret_value())
        verifier = SampleVerifier(rpc, factory, max_rps=settings.verify_max_rps)
    return IngestService(settings, provider, IngestStore(factory), factory, alerter, verifier)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url.get_secret_value(), settings.db_pool_size)
        factory = create_session_factory(engine)
        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        alerter = build_alerter(settings, client)
        checks = database_checks(engine, expected_schema_head())
        ingest: IngestService | None = None
        if settings.ingest_enabled:
            ingest = build_ingest(settings, factory, client, alerter)
            service = ingest

            async def feed_ready() -> bool:
                return service.feed_ready()

            checks["feed"] = feed_ready
        probe = ReadinessProbe(checks)
        app.state.readiness = probe
        app.state.ingest = ingest

        tasks: list[asyncio.Task[None]] = []
        if settings.healthchecks_ping_url is not None:
            heartbeat = Heartbeat(
                client, settings.healthchecks_ping_url.get_secret_value(), probe.is_ready
            )
            tasks.append(asyncio.create_task(heartbeat.run(settings.heartbeat_interval_seconds)))

        payload = {
            "execution_mode": settings.execution_mode.value,
            "env": settings.app_env,
            "ingest_enabled": settings.ingest_enabled,
        }
        if ingest is not None:
            await ingest.start()
        await record_system_event(factory, "service_started", Severity.INFO, payload)
        await alerter.send(
            Severity.INFO, "Trading desk démarré", f"mode={payload['execution_mode']}"
        )
        log.info("service_started", **payload)
        try:
            yield
        finally:
            if ingest is not None:
                await ingest.stop()
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
