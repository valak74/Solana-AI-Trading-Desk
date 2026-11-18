"""Async engine and session factory."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _json_default(value: Any) -> str:
    # Decimals are stored as strings to keep every digit (JSON numbers would go through float).
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


def create_engine(database_url: str, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        database_url, pool_size=pool_size, pool_pre_ping=True, json_serializer=json_dumps
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Session wrapped in a transaction: commit on success, rollback on any error."""
    async with factory() as session, session.begin():
        yield session


async def database_is_reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
