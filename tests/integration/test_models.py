from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.database.enums import ExecutionModeDb, PositionStatus, Severity, TokenStatus
from backend.database.models import Position, RawEvent, SystemEvent, Token, TokenSnapshot
from backend.database.session import create_engine, create_session_factory, transaction
from backend.database.system_events import record_system_event

MINT = "So11111111111111111111111111111111111111112"
Factory = async_sessionmaker[AsyncSession]


async def add_token(factory: Factory) -> int:
    async with transaction(factory) as session:
        token = Token(mint_address=MINT, decimals=6)
        session.add(token)
        await session.flush()
        return token.id


async def test_token_defaults(session_factory: Factory) -> None:
    await add_token(session_factory)
    async with session_factory() as session:
        token = (await session.execute(select(Token))).scalar_one()
    assert token.status is TokenStatus.DISCOVERED
    assert token.first_seen_at.tzinfo is not None


async def test_prices_and_amounts_are_exact(session_factory: Factory) -> None:
    token_id = await add_token(session_factory)
    price = Decimal("0.000000028012345678")
    big = 2**64 - 1
    async with transaction(session_factory) as session:
        session.add(
            TokenSnapshot(
                token_id=token_id,
                timestamp=datetime.now(UTC),
                price_sol=price,
                virtual_token_reserves=big,
                liquidity_lamports=9_000_000_000_000_000_000,
            )
        )
    async with session_factory() as session:
        snap = (await session.execute(select(TokenSnapshot))).scalar_one()
    assert snap.price_sol == price
    assert snap.virtual_token_reserves == big
    assert isinstance(snap.virtual_token_reserves, int)


async def test_raw_events_are_deduplicated(session_factory: Factory) -> None:
    def event() -> RawEvent:
        return RawEvent(
            source="test",
            event_type="token_created",
            dedup_key="sig1:0",
            received_at=datetime.now(UTC),
            payload={"a": 1},
        )

    async with transaction(session_factory) as session:
        session.add(event())
    with pytest.raises(IntegrityError):
        async with transaction(session_factory) as session:
            session.add(event())


async def test_invalid_enum_value_rejected_by_database(engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tokens (mint_address, status) VALUES (:m, 'moon')"), {"m": MINT}
            )


async def test_position_remaining_cannot_exceed_quantity(session_factory: Factory) -> None:
    token_id = await add_token(session_factory)
    with pytest.raises(IntegrityError):
        async with transaction(session_factory) as session:
            session.add(
                Position(
                    token_id=token_id,
                    strategy="A",
                    execution_mode=ExecutionModeDb.PAPER,
                    entry_price=Decimal("0.00000003"),
                    quantity=100,
                    remaining_quantity=101,
                    invested_lamports=1_000,
                    status=PositionStatus.OPEN,
                )
            )


async def test_negative_quantity_rejected_by_database(engine: AsyncEngine) -> None:
    token_id = 1
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tokens (id, mint_address, status) VALUES (1, :m, 'discovered')"),
            {"m": MINT},
        )
    with pytest.raises((IntegrityError, DBAPIError)):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO positions (token_id, strategy, execution_mode, entry_price,"
                    " quantity, remaining_quantity, invested_lamports, status) VALUES "
                    "(:t, 'A', 'paper', 1, -5, -5, 0, 'open')"
                ),
                {"t": token_id},
            )


async def test_record_system_event(session_factory: Factory) -> None:
    assert await record_system_event(session_factory, "unit", Severity.WARNING, {"k": "v"})
    async with session_factory() as session:
        event = (await session.execute(select(SystemEvent))).scalar_one()
    assert event.severity is Severity.WARNING
    assert event.payload == {"k": "v"}


async def test_record_system_event_survives_database_outage() -> None:
    dead = create_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    try:
        assert not await record_system_event(create_session_factory(dead), "x", Severity.INFO)
    finally:
        await dead.dispose()
