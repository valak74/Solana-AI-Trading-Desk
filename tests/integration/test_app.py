import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.database.models import SystemEvent
from backend.main import create_app


def settings_for(url: str) -> Settings:
    return Settings(_env_file=None, database_url=SecretStr(url), app_env="test")


async def test_service_lifecycle_and_readiness(
    database_url: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    app = create_app(settings_for(database_url))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == {"database": True, "schema": True}

    async with session_factory() as session:
        events = (
            (await session.execute(select(SystemEvent.event_type).order_by(SystemEvent.id)))
            .scalars()
            .all()
        )
    assert events == ["service_started", "service_stopped"]


async def test_not_ready_when_database_unreachable() -> None:
    app = create_app(settings_for("postgresql+asyncpg://nobody:x@127.0.0.1:1/none"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/live")).status_code == 200
            ready = await client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json()["checks"]["database"] is False
