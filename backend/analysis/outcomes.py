"""Outcome labels: what happened to each analysed token after its first analysis.

For horizons of 5, 15, 30 and 60 minutes, records the reference price at the first analysis and
the max / min / last price observed afterwards (recorded curve snapshots), whether the token
graduated within the horizon, and whether a data gap overlaps the window (such labels must be
excluded from evaluation). These labels allow testing other entry rules offline on every
candidate, not only on the tokens a strategy actually traded.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.models import (
    FeedGap,
    RawEvent,
    SecurityAnalysis,
    Token,
    TokenOutcome,
    TokenSnapshot,
)
from backend.database.session import transaction
from backend.monitoring.logging import get_logger

log = get_logger(__name__)

HORIZONS = (5, 15, 30, 60)
REFERENCE_LOOKBACK = timedelta(minutes=5)
Q = Decimal("0.000001")


def _ret(price: Decimal | None, ref: Decimal | None) -> Decimal | None:
    if price is None or ref is None or ref <= 0:
        return None
    return (price / ref - 1).quantize(Q)


class OutcomeLabeler:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        batch: int = 200,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._batch = batch

    async def run(self, interval: float = 60.0) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                log.error("outcome_labeling_failed", error_type=type(exc).__name__)
            await asyncio.sleep(interval)

    async def run_once(self) -> int:
        now = self._clock()
        done = 0
        for horizon in HORIZONS:
            for token_id, mint, reference_at in await self._due(now, horizon):
                await self._label(token_id, mint, reference_at, horizon, now)
                done += 1
        return done

    async def _due(self, now: datetime, horizon: int) -> list[tuple[int, str, datetime]]:
        first = (
            select(SecurityAnalysis.token_id, func.min(SecurityAnalysis.created_at).label("ref"))
            .group_by(SecurityAnalysis.token_id)
            .subquery()
        )
        labelled = exists().where(
            TokenOutcome.token_id == first.c.token_id, TokenOutcome.horizon_minutes == horizon
        )
        query = (
            select(first.c.token_id, Token.mint_address, first.c.ref)
            .join(Token, Token.id == first.c.token_id)
            .where(first.c.ref <= now - timedelta(minutes=horizon), ~labelled)
            .order_by(first.c.ref)
            .limit(self._batch)
        )
        async with self._factory() as session:
            return [(r[0], r[1], r[2]) for r in (await session.execute(query)).all()]

    async def _label(
        self, token_id: int, mint: str, reference_at: datetime, horizon: int, now: datetime
    ) -> None:
        end = reference_at + timedelta(minutes=horizon)
        async with transaction(self._factory) as session:
            reference = (
                await session.execute(
                    select(TokenSnapshot.price_sol)
                    .where(
                        TokenSnapshot.token_id == token_id,
                        TokenSnapshot.timestamp <= reference_at,
                        TokenSnapshot.timestamp >= reference_at - REFERENCE_LOOKBACK,
                        TokenSnapshot.price_sol.is_not(None),
                    )
                    .order_by(TokenSnapshot.timestamp.desc())
                    .limit(1)
                )
            ).scalar()
            window = (
                (
                    await session.execute(
                        select(TokenSnapshot.price_sol)
                        .where(
                            TokenSnapshot.token_id == token_id,
                            TokenSnapshot.timestamp > reference_at,
                            TokenSnapshot.timestamp <= end,
                            TokenSnapshot.price_sol.is_not(None),
                        )
                        .order_by(TokenSnapshot.timestamp)
                    )
                )
                .scalars()
                .all()
            )
            graduated = (
                await session.execute(
                    select(
                        exists().where(
                            RawEvent.event_type == "migration",
                            RawEvent.payload["mint"].astext == mint,
                            RawEvent.received_at <= end,
                        )
                    )
                )
            ).scalar()
            gap = (
                await session.execute(
                    select(
                        exists().where(
                            FeedGap.started_at < end,
                            or_(FeedGap.ended_at.is_(None), and_(FeedGap.ended_at > reference_at)),
                        )
                    )
                )
            ).scalar()
            prices = [p for p in window if p is not None]
            session.add(
                TokenOutcome(
                    token_id=token_id,
                    horizon_minutes=horizon,
                    reference_at=reference_at,
                    reference_price=reference,
                    max_price=max(prices) if prices else None,
                    min_price=min(prices) if prices else None,
                    last_price=prices[-1] if prices else None,
                    max_return=_ret(max(prices), reference) if prices else None,
                    min_return=_ret(min(prices), reference) if prices else None,
                    last_return=_ret(prices[-1], reference) if prices else None,
                    graduated=bool(graduated),
                    snapshots=len(prices),
                    gap_overlap=bool(gap),
                    computed_at=now,
                )
            )
