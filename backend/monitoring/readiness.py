"""Readiness: the service may only report healthy when its dependencies are usable."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.database.session import database_is_reachable

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

Check = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class ReadinessReport:
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


class ReadinessProbe:
    def __init__(self, checks: dict[str, Check]) -> None:
        self._checks = checks

    async def report(self) -> ReadinessReport:
        results: dict[str, bool] = {}
        for name, check in self._checks.items():
            try:
                results[name] = await check()
            except Exception:
                results[name] = False
        return ReadinessReport(results)

    async def is_ready(self) -> bool:
        return (await self.report()).ready


def expected_schema_head(migrations_dir: Path = MIGRATIONS_DIR) -> str | None:
    return ScriptDirectory(str(migrations_dir)).get_current_head()


def database_checks(engine: AsyncEngine, expected_head: str | None) -> dict[str, Check]:
    async def reachable() -> bool:
        return await database_is_reachable(engine)

    async def schema_up_to_date() -> bool:
        async with engine.connect() as conn:
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        return current is not None and current == expected_head

    return {"database": reachable, "schema": schema_up_to_date}
