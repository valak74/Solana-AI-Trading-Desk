import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from backend.database.models import Base
from backend.database.session import create_engine


async def table_names(url: str) -> set[str]:
    engine = create_engine(url)
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    await engine.dispose()
    return names


def test_downgrade_then_upgrade_roundtrip(migrated: Config, database_url: str) -> None:
    command.downgrade(migrated, "base")
    assert asyncio.run(table_names(database_url)) <= {"alembic_version"}

    command.upgrade(migrated, "head")
    expected = {t.name for t in Base.metadata.sorted_tables}
    assert expected <= asyncio.run(table_names(database_url))


async def test_models_match_migrations(migrated: Config, database_url: str) -> None:
    """Fails if a model was changed without generating the corresponding migration."""

    def diff(conn: Connection) -> list[Any]:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    engine = create_engine(database_url)
    async with engine.connect() as conn:
        differences = await conn.run_sync(diff)
    await engine.dispose()
    assert differences == []
