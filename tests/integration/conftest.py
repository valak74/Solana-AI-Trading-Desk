"""Integration fixtures. Requires TEST_DATABASE_URL pointing to a disposable PostgreSQL database.

Example: TEST_DATABASE_URL=postgresql+asyncpg://desk:desk@localhost:5432/desk_test
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.database.models import Base
from backend.database.session import create_engine, create_session_factory

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.integration)
            if TEST_DATABASE_URL is None:
                item.add_marker(pytest.mark.skip(reason="TEST_DATABASE_URL not set"))


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def database_url() -> str:
    assert TEST_DATABASE_URL is not None
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated(database_url: str) -> Iterator[Config]:
    cfg = alembic_config(database_url)
    command.upgrade(cfg, "head")
    yield cfg


@pytest.fixture
async def engine(migrated: Config, database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(database_url)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)
