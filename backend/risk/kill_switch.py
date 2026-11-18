"""Persistent kill switch.

When active, the risk manager vetoes every new position (paper now, live later). It does not
liquidate open positions: their exits keep following the strategy's rules. If the state cannot
be read, the switch is considered ACTIVE: in doubt, no new trade.

    python -m backend.risk.kill_switch status
    python -m backend.risk.kill_switch on --reason "manual stop"
    python -m backend.risk.kill_switch off
"""

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import Severity
from backend.database.models import KillSwitchState
from backend.database.session import create_engine, create_session_factory, transaction
from backend.database.system_events import record_system_event


async def is_active(factory: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with factory() as session:
            row = (await session.execute(select(KillSwitchState.active))).scalar()
    except Exception:
        return True  # unreadable state: refuse new positions
    return bool(row)


async def set_state(
    factory: async_sessionmaker[AsyncSession], active: bool, reason: str | None
) -> None:
    now = datetime.now(UTC)
    async with transaction(factory) as session:
        await session.execute(
            insert(KillSwitchState)
            .values(id=1, active=active, reason=reason, updated_at=now)
            .on_conflict_do_update(
                index_elements=["id"], set_={"active": active, "reason": reason, "updated_at": now}
            )
        )
    await record_system_event(
        factory,
        "kill_switch_on" if active else "kill_switch_off",
        Severity.CRITICAL if active else Severity.WARNING,
        {"reason": reason},
    )


async def _main(command: str, reason: str | None) -> str:
    from backend.config import get_settings

    engine = create_engine(get_settings().database_url.get_secret_value())
    factory = create_session_factory(engine)
    try:
        if command in {"on", "off"}:
            await set_state(factory, command == "on", reason)
        return "ACTIVE" if await is_active(factory) else "inactive"
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent kill switch")
    parser.add_argument("command", choices=["status", "on", "off"])
    parser.add_argument("--reason", default=None)
    args = parser.parse_args()
    print(f"kill switch: {asyncio.run(_main(args.command, args.reason))}")


if __name__ == "__main__":
    main()
