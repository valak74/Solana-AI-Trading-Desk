import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import PoolStatus, TokenStatus
from backend.database.models import FeedGap, Pool, RawEvent, Token, TokenSnapshot
from backend.scanner.feed_monitor import FeedMonitor
from backend.scanner.processor import IngestProcessor
from backend.scanner.providers.base import ConnectionStatus, RawMessage
from backend.scanner.store import IngestStore
from backend.scanner.tracking import TrackingRegistry
from tests.fixtures.pumpportal import create_msg, migration_msg, mint, sig, trade_msg

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
Factory = async_sessionmaker[AsyncSession]


def processor() -> IngestProcessor:
    reg = TrackingRegistry(
        timedelta(minutes=60), timedelta(minutes=360), 5, timedelta(minutes=10), 100
    )
    return IngestProcessor(reg, FeedMonitor(timedelta(seconds=60)), rng=random.Random(0))


def raw(payload: dict[str, object], seconds: float) -> RawMessage:
    return RawMessage("pumpportal", T0 + timedelta(seconds=seconds), dict(payload), 200)


async def count(factory: Factory, model: type[object]) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_full_batch_is_persisted(session_factory: Factory) -> None:
    p = processor()
    p.handle(ConnectionStatus("pumpportal", True, T0))
    p.handle(raw(create_msg(mint(1), sig(1)), 0))
    p.handle(raw(trade_msg(mint(1), sig(2)), 6))
    p.handle(raw(trade_msg(mint(1), sig(3), "sell"), 12))
    p.handle(raw(migration_msg(mint(1), sig(4)), 20))
    store = IngestStore(session_factory)

    assert await store.write(p.take_batch()) == 4

    async with session_factory() as session:
        token = (await session.execute(select(Token))).scalar_one()
        pool = (await session.execute(select(Pool))).scalar_one()
        snaps = (await session.execute(select(TokenSnapshot))).scalars().all()
        create_row = (
            await session.execute(select(RawEvent).where(RawEvent.event_type == "token_created"))
        ).scalar_one()
    assert (token.symbol, token.decimals, token.status) == ("TEST", 6, TokenStatus.GRADUATED)
    assert pool.status is PoolStatus.MIGRATED
    assert len(snaps) == 3
    assert snaps[-1].sell_count == 1
    # Decimal amounts are stored as exact strings inside the JSON payload.
    assert create_row.payload["solAmount"] == "1.000000001"
    assert await store.last_event_time() == T0 + timedelta(seconds=20)


async def test_rewriting_same_events_inserts_nothing(session_factory: Factory) -> None:
    store = IngestStore(session_factory)
    first = processor()
    first.handle(raw(create_msg(mint(1), sig(1)), 0))
    assert await store.write(first.take_batch()) == 1
    replay = processor()  # fresh in-memory dedup: only the database constraint protects us
    replay.handle(raw(create_msg(mint(1), sig(1)), 0))
    assert await store.write(replay.take_batch()) == 0
    assert await count(session_factory, RawEvent) == 1
    assert await count(session_factory, Token) == 1


async def test_trade_before_creation_metadata_filled_later(session_factory: Factory) -> None:
    store = IngestStore(session_factory)
    p = processor()
    p.handle(raw(trade_msg(mint(2), sig(5)), 0))
    await store.write(p.take_batch())
    p.handle(raw(create_msg(mint(2), sig(6)), 1))
    await store.write(p.take_batch())
    async with session_factory() as session:
        token = (await session.execute(select(Token))).scalar_one()
    assert token.name == "Test Coin"
    assert token.status is TokenStatus.DISCOVERED
    assert token.first_seen_at == T0  # earliest sighting kept, not the creation message time


async def test_gap_opened_and_closed_across_batches(session_factory: Factory) -> None:
    store = IngestStore(session_factory)
    p = processor()
    p.handle(ConnectionStatus("pumpportal", True, T0))
    p.handle(raw(create_msg(mint(1), sig(1)), 0))
    p.handle(ConnectionStatus("pumpportal", False, T0 + timedelta(seconds=5), "OSError"))
    await store.write(p.take_batch())
    async with session_factory() as session:
        gap = (await session.execute(select(FeedGap))).scalar_one()
    assert gap.ended_at is None and gap.reason == "disconnected:OSError"

    p.handle(ConnectionStatus("pumpportal", True, T0 + timedelta(seconds=30)))
    p.handle(raw(trade_msg(mint(1), sig(2)), 31))
    await store.write(p.take_batch())
    async with session_factory() as session:
        gap = (await session.execute(select(FeedGap))).scalar_one()
    assert gap.started_at == T0
    assert gap.ended_at == T0 + timedelta(seconds=31)
