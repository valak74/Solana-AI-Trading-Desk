from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.server import serve

from backend.config import Settings
from backend.database.enums import Severity
from backend.database.models import FeedGap, RawEvent, SystemEvent, Token
from backend.main import create_app
from backend.scanner.providers.base import ConnectionStatus, ProviderItem, RawMessage
from backend.scanner.providers.replay import ReplayProvider
from backend.scanner.service import IngestService
from backend.scanner.store import IngestStore
from tests.fixtures.fake_pumpportal import FakePumpPortal
from tests.fixtures.helpers import wait_until, wait_until_async
from tests.fixtures.pumpportal import create_msg, create_text, mint, sig, trade_msg

Factory = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str]] = []

    async def send(self, severity: Severity, title: str, body: str = "") -> bool:
        self.sent.append((severity, title))
        return True


def settings(url: str, **extra: object) -> Settings:
    return Settings(
        _env_file=None,
        database_url=SecretStr(url),
        app_env="test",
        **extra,  # type: ignore[arg-type]
    )


async def test_service_records_replayed_feed_and_alerts_on_gaps(
    database_url: str, session_factory: Factory
) -> None:
    items: list[ProviderItem] = [
        ConnectionStatus("pumpportal", True, T0),
        RawMessage("pumpportal", T0, create_msg(mint(1), sig(1)), 100),
        RawMessage("pumpportal", T0 + timedelta(seconds=6), trade_msg(mint(1), sig(2)), 100),
        ConnectionStatus("pumpportal", False, T0 + timedelta(seconds=7), "ConnectionClosed"),
        ConnectionStatus("pumpportal", True, T0 + timedelta(seconds=20)),
        RawMessage("pumpportal", T0 + timedelta(seconds=21), trade_msg(mint(1), sig(3)), 100),
    ]
    provider = ReplayProvider(items)
    alerter = RecordingAlerter()
    service = IngestService(
        settings(database_url),
        provider,
        IngestStore(session_factory),
        session_factory,
        alerter,
        flush_interval=0.05,
        tick_interval=3600,
    )
    await service.start()
    await wait_until(lambda: service.processor.stats.messages == 3)
    await service.record_stats()
    await service.stop()

    assert provider.track_calls == [[mint(1)]]
    async with session_factory() as session:
        raw_count = (await session.execute(select(func.count()).select_from(RawEvent))).scalar()
        gap = (await session.execute(select(FeedGap))).scalar_one()
        stats = (
            await session.execute(
                select(SystemEvent).where(SystemEvent.event_type == "ingest_stats")
            )
        ).scalar_one()
    assert raw_count == 3
    assert gap.ended_at == T0 + timedelta(seconds=21)
    assert stats.payload["messages"] == 3
    assert stats.payload["raw_events_table_bytes"] > 0
    titles = [title for _, title in alerter.sent]
    assert titles == ["Flux pumpportal interrompu", "Flux pumpportal rétabli"]


async def test_app_end_to_end_with_local_fake_feed(
    database_url: str, session_factory: Factory
) -> None:
    fake = FakePumpPortal()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        fake.server = server
        app = create_app(
            settings(
                database_url,
                ingest_enabled=True,
                pumpportal_ws_url=fake.url,
                feed_stale_after_seconds=30,
            )
        )
        async with app.router.lifespan_context(app):
            await wait_until(lambda: fake.connections == 1)
            await fake.to_send.put(create_text(mint(7), sig(7)))
            await wait_until(
                lambda: any(m.get("method") == "subscribeTokenTrade" for m in fake.received)
            )

            async def token_stored() -> bool:
                async with session_factory() as session:
                    found = await session.execute(
                        select(Token).where(Token.mint_address == mint(7))
                    )
                    return found.scalar_one_or_none() is not None

            await wait_until_async(token_stored)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                ready = await client.get("/health/ready")
            assert ready.status_code == 200
            assert ready.json()["checks"] == {"database": True, "schema": True, "feed": True}
            await fake.to_send.put(None)
    subscribed = [m for m in fake.received if m.get("method") == "subscribeTokenTrade"]
    assert subscribed[0]["keys"] == [mint(7)]


async def test_app_starts_with_analysis_enabled(database_url: str) -> None:
    app = create_app(
        settings(
            database_url,
            ingest_enabled=True,
            pumpportal_ws_url="ws://127.0.0.1:1",  # nothing listens: ingestion keeps retrying
            helius_api_key=SecretStr("dummy"),
            helius_rpc_url="http://127.0.0.1:1",
            analysis_interval_seconds=0.05,
        )
    )
    async with app.router.lifespan_context(app):
        analysis = app.state.analysis
        assert analysis is not None
        assert app.state.paper is not None  # paper trading wired to the live stream
        assert analysis.on_result == app.state.paper.on_analysis
        await wait_until(lambda: analysis.analyzed == 0 and analysis._config_id is not None)
