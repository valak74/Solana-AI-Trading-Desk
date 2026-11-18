"""Persistence of ingestion batches: one transaction per batch, idempotent inserts."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import PoolKind, PoolStatus, TokenStatus
from backend.database.models import FeedGap, Pool, RawEvent, Token, TokenSnapshot
from backend.database.session import transaction
from backend.scanner.feed_monitor import GapClosed, GapEvent, GapOpened
from backend.scanner.processor import WriteBatch

_TOKEN_METADATA = ("creator_wallet", "name", "symbol", "metadata_uri", "launchpad", "decimals")
_INSERT_CHUNK = 1000


def _chunks(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [rows[i : i + _INSERT_CHUNK] for i in range(0, len(rows), _INSERT_CHUNK)]


class IngestStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def last_event_time(self) -> datetime | None:
        async with self._factory() as session:
            result: datetime | None = (
                await session.execute(select(func.max(RawEvent.received_at)))
            ).scalar()
        return result

    async def raw_events_size_bytes(self) -> int:
        async with self._factory() as session:
            size = (
                await session.execute(text("SELECT pg_total_relation_size('raw_events')"))
            ).scalar()
        return int(size or 0)

    async def write(self, batch: WriteBatch) -> int:
        """Persist a batch atomically. Returns the number of new raw events stored."""
        async with transaction(self._factory) as session:
            inserted = await self._insert_raw(session, batch.raw_events)
            await self._upsert_tokens(session, list(batch.tokens.values()))
            ids = await self._token_ids(session, self._referenced_mints(batch))
            await self._insert_pools(session, batch, ids)
            await self._mark_graduated(session, batch.graduated, ids)
            await self._insert_snapshots(session, batch, ids)
            for source, gap in batch.gaps:
                await self._apply_gap(session, source, gap)
        return inserted

    @staticmethod
    def _referenced_mints(batch: WriteBatch) -> set[str]:
        mints = {row["mint"] for row in batch.pools.values()}
        mints |= {row["mint"] for row in batch.snapshots}
        return mints | batch.graduated

    @staticmethod
    async def _insert_raw(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
        inserted = 0
        for chunk in _chunks(rows):
            stmt = (
                insert(RawEvent)
                .values(chunk)
                .on_conflict_do_nothing(index_elements=["dedup_key"])
                .returning(RawEvent.id)
            )
            inserted += len((await session.execute(stmt)).all())
        return inserted

    @staticmethod
    async def _upsert_tokens(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        columns = ("mint_address", "first_seen_at", *_TOKEN_METADATA)
        normalized = [{c: row.get(c) for c in columns} for row in rows]
        for chunk in _chunks(normalized):
            stmt = insert(Token).values(chunk)
            # Fill metadata that was unknown (e.g. trade seen before creation); never overwrite.
            stmt = stmt.on_conflict_do_update(
                index_elements=["mint_address"],
                set_={
                    c: func.coalesce(getattr(Token, c), stmt.excluded[c]) for c in _TOKEN_METADATA
                }
                | {"first_seen_at": func.least(Token.first_seen_at, stmt.excluded.first_seen_at)},
            )
            await session.execute(stmt)

    @staticmethod
    async def _token_ids(session: AsyncSession, mints: set[str]) -> dict[str, int]:
        if not mints:
            return {}
        rows = await session.execute(
            select(Token.mint_address, Token.id).where(Token.mint_address.in_(mints))
        )
        return {mint: token_id for mint, token_id in rows.all()}

    @staticmethod
    async def _insert_pools(session: AsyncSession, batch: WriteBatch, ids: dict[str, int]) -> None:
        rows = [
            {
                "token_id": ids[row["mint"]],
                "address": row["address"],
                "program_id": row["program_id"],
                "kind": PoolKind(row["kind"]),
                "first_seen_at": row["first_seen_at"],
            }
            for row in batch.pools.values()
            if row["mint"] in ids
        ]
        for chunk in _chunks(rows):
            await session.execute(
                insert(Pool).values(chunk).on_conflict_do_nothing(index_elements=["address"])
            )

    @staticmethod
    async def _mark_graduated(session: AsyncSession, mints: set[str], ids: dict[str, int]) -> None:
        token_ids = [ids[m] for m in mints if m in ids]
        if not token_ids:
            return
        await session.execute(
            update(Token).where(Token.id.in_(token_ids)).values(status=TokenStatus.GRADUATED)
        )
        await session.execute(
            update(Pool)
            .where(Pool.token_id.in_(token_ids), Pool.kind == PoolKind.BONDING_CURVE)
            .values(status=PoolStatus.MIGRATED)
        )

    @staticmethod
    async def _insert_snapshots(
        session: AsyncSession, batch: WriteBatch, ids: dict[str, int]
    ) -> None:
        rows = []
        for snap in batch.snapshots:
            token_id = ids.get(snap["mint"])
            if token_id is not None:
                rows.append({k: v for k, v in snap.items() if k != "mint"} | {"token_id": token_id})
        for chunk in _chunks(rows):
            await session.execute(insert(TokenSnapshot).values(chunk))

    @staticmethod
    async def _apply_gap(session: AsyncSession, source: str, gap: GapEvent) -> None:
        if isinstance(gap, GapOpened):
            session.add(FeedGap(source=source, reason=gap.reason, started_at=gap.started_at))
            await session.flush()
            return
        if not isinstance(gap, GapClosed):
            return
        result = await session.execute(
            update(FeedGap)
            .where(FeedGap.source == source, FeedGap.ended_at.is_(None))
            .values(ended_at=gap.ended_at)
            .returning(FeedGap.id)
        )
        if not result.all():
            session.add(
                FeedGap(
                    source=source,
                    reason=gap.reason,
                    started_at=gap.started_at,
                    ended_at=gap.ended_at,
                )
            )
            await session.flush()
