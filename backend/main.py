"""Application assembly: one process running the API and background tasks (measurement stage)."""

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.analysis.engine import TokenAnalyzer
from backend.analysis.outcomes import OutcomeLabeler
from backend.analysis.repository import AnalysisRepository
from backend.analysis.service import AnalysisService
from backend.api.health import router as health_router
from backend.config import Settings, get_settings
from backend.database.enums import Severity
from backend.database.session import create_engine, create_session_factory
from backend.database.system_events import record_system_event
from backend.execution.paper import PaperExecutor
from backend.integrations.helius import CreditBudget, HeliusRpc
from backend.integrations.jupiter import JupiterClient
from backend.integrations.rugcheck import RugCheckClient
from backend.monitoring.alerts import Alerter, LogOnlyAlerter, TelegramAlerter
from backend.monitoring.heartbeat import Heartbeat
from backend.monitoring.logging import configure_logging, get_logger
from backend.monitoring.readiness import ReadinessProbe, database_checks, expected_schema_head
from backend.risk.manager import RiskManager
from backend.scanner.providers.pumpportal import PumpPortalProvider
from backend.scanner.service import IngestService
from backend.scanner.store import IngestStore
from backend.scanner.verifier import SampleVerifier
from backend.security.config import AnalysisConfig
from backend.trading.config import PaperTradingConfig
from backend.trading.engine import PaperTradingEngine
from backend.trading.repository import SqlTradingStore
from backend.trading.service import PaperTradingService

log = get_logger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0


def build_alerter(settings: Settings, client: httpx.AsyncClient) -> Alerter:
    if settings.telegram_bot_token is not None and settings.telegram_chat_id is not None:
        return TelegramAlerter(
            client, settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
        )
    return LogOnlyAlerter()


def build_rpc(settings: Settings, client: httpx.AsyncClient) -> HeliusRpc | None:
    """One RPC client and one credit budget shared by every component."""
    if settings.helius_api_key is None:
        return None
    budget = CreditBudget(settings.helius_daily_credit_budget)
    return HeliusRpc(
        client, settings.helius_rpc_url, settings.helius_api_key.get_secret_value(), budget
    )


def build_ingest(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    alerter: Alerter,
    rpc: HeliusRpc | None,
) -> IngestService:
    key = settings.pumpportal_api_key
    provider = PumpPortalProvider(
        settings.pumpportal_ws_url, key.get_secret_value() if key else None
    )
    verifier = None
    if rpc is not None and settings.verify_sample_rate > 0:
        verifier = SampleVerifier(rpc, factory, max_rps=settings.verify_max_rps)
    return IngestService(settings, provider, IngestStore(factory), factory, alerter, verifier)


def build_analysis(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    alerter: Alerter,
    rpc: HeliusRpc,
) -> AnalysisService:
    config = AnalysisConfig()
    rugcheck = RugCheckClient(client) if settings.rugcheck_enabled else None
    if rpc.budget is None:
        raise ValueError("the analysis requires an RPC client with a credit budget")
    return AnalysisService(
        TokenAnalyzer(rpc, config, rugcheck),
        AnalysisRepository(factory),
        factory,
        config,
        rpc.budget,
        alerter,
        interval=settings.analysis_interval_seconds,
        min_age=timedelta(minutes=settings.analysis_min_age_minutes),
        max_age=timedelta(minutes=settings.analysis_max_age_minutes),
        reanalyze_after=timedelta(minutes=settings.analysis_reanalyze_minutes),
        min_trades=settings.analysis_min_trades,
        batch=settings.analysis_batch,
    )


def build_paper_trading(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    ingest: IngestService,
) -> PaperTradingService:
    config = PaperTradingConfig()
    jupiter = None
    if settings.jupiter_api_key is not None:
        jupiter = JupiterClient(
            client, settings.jupiter_api_key.get_secret_value(), settings.jupiter_base_url
        )
    service = PaperTradingService(factory, tick_interval=settings.paper_tick_seconds)
    risk = RiskManager(config.risk, service.kill_switch_active, lambda _now: ingest.feed_ready())
    executor = PaperExecutor(
        config.execution,
        random.Random(f"{config.seed}:execution"),  # noqa: S311 - simulation only
        jupiter,
    )
    service.attach(PaperTradingEngine(config, executor, risk, SqlTradingStore(factory)))
    return service


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
        rpc = build_rpc(settings, client)
        ingest: IngestService | None = None
        analysis: AnalysisService | None = None
        paper: PaperTradingService | None = None
        if settings.ingest_enabled:
            ingest = build_ingest(settings, factory, alerter, rpc)
            service = ingest
            if settings.analysis_enabled and rpc is not None:
                analysis = build_analysis(settings, factory, client, alerter, rpc)
            if settings.paper_trading_enabled:
                paper = build_paper_trading(settings, factory, client, ingest)
                ingest.listeners.append(paper.on_raw)
                ingest.pinned = paper.pinned_mints
                if analysis is not None:
                    analysis.on_result = paper.on_analysis

            async def feed_ready() -> bool:
                return service.feed_ready()

            checks["feed"] = feed_ready
        probe = ReadinessProbe(checks)
        app.state.readiness = probe
        app.state.ingest = ingest
        app.state.analysis = analysis
        app.state.paper = paper

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
            "analysis_enabled": analysis is not None,
            "paper_trading_enabled": paper is not None,
        }
        if ingest is not None:
            await ingest.start()
        if paper is not None:
            await paper.start()
            tasks.append(asyncio.create_task(paper.run()))
        if analysis is not None:
            tasks.append(asyncio.create_task(analysis.run()))
        if ingest is not None:
            tasks.append(asyncio.create_task(OutcomeLabeler(factory).run()))
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
