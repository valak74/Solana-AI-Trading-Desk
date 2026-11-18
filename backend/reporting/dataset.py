"""Loading of the measured results from the database (the report is rebuilt from it alone)."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import ExecutionModeDb, PositionStatus, TradeSide, TradeStatus
from backend.database.models import (
    FeedGap,
    Position,
    RawEvent,
    Token,
    TokenOutcome,
    TokenScore,
    Trade,
)
from backend.trading.repository import INTERRUPTED

Factory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class PositionResult:
    strategy: str
    mint: str
    opened_at: datetime
    closed_at: datetime
    pnl: int | None
    cost: int
    exit_reason: str
    network_fees: int
    protocol_fees: int
    slippage: Decimal | None  # mean relative shortfall of fills versus decision-time quotes
    fill_attempts: int
    fill_failures: int
    score_at_entry: Decimal | None

    @property
    def filled(self) -> bool:
        return not self.exit_reason.startswith("entry_failed")

    @property
    def interrupted(self) -> bool:
        return self.exit_reason == INTERRUPTED


@dataclass(frozen=True, slots=True)
class OutcomeLabel:
    band: str
    horizon: int
    max_return: Decimal | None
    min_return: Decimal | None
    last_return: Decimal | None
    graduated: bool
    gap_overlap: bool


def _shortfall(trade: Trade) -> Decimal | None:
    if trade.status is not TradeStatus.CONFIRMED or not trade.expected_out or not trade.actual_out:
        return None
    return 1 - Decimal(trade.actual_out) / Decimal(trade.expected_out)


async def measurement_start(factory: Factory) -> datetime | None:
    async with factory() as session:
        first: datetime | None = (
            await session.execute(select(func.min(RawEvent.received_at)))
        ).scalar()
    return first


async def load_positions(
    factory: Factory, run_id: str, start: datetime, end: datetime
) -> list[PositionResult]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(Position, Token.mint_address)
                .join(Token, Token.id == Position.token_id)
                .where(
                    Position.run_id == run_id,
                    Position.execution_mode == ExecutionModeDb.PAPER,
                    Position.status == PositionStatus.CLOSED,
                    Position.opened_at >= start,
                    Position.opened_at < end,
                )
                .order_by(Position.closed_at)
            )
        ).all()
        ids = [p.id for p, _ in rows]
        trades_by_position: defaultdict[int, list[Trade]] = defaultdict(list)
        if ids:
            for trade in (
                await session.execute(select(Trade).where(Trade.position_id.in_(ids)))
            ).scalars():
                trades_by_position[trade.position_id].append(trade)
        token_ids = {p.token_id for p, _ in rows}
        scores: defaultdict[int, list[tuple[datetime, Decimal]]] = defaultdict(list)
        if token_ids:
            for token_id, created_at, total in (
                await session.execute(
                    select(TokenScore.token_id, TokenScore.created_at, TokenScore.total_score)
                    .where(TokenScore.token_id.in_(token_ids))
                    .order_by(TokenScore.created_at)
                )
            ).all():
                scores[token_id].append((created_at, total))

    results = []
    for position, mint in rows:
        trades = trades_by_position[position.id]
        shortfalls = [s for t in trades if (s := _shortfall(t)) is not None]
        before = [total for at, total in scores[position.token_id] if at <= position.opened_at]
        results.append(
            PositionResult(
                strategy=position.strategy,
                mint=mint,
                opened_at=position.opened_at,
                closed_at=position.closed_at or position.opened_at,
                pnl=position.realized_pnl_lamports,
                cost=position.invested_lamports,
                exit_reason=position.exit_reason or "unknown",
                network_fees=sum(t.network_fee_lamports for t in trades),
                protocol_fees=sum(t.fees_lamports for t in trades),
                slippage=sum(shortfalls, Decimal(0)) / len(shortfalls) if shortfalls else None,
                fill_attempts=len(trades),
                fill_failures=sum(1 for t in trades if t.status is TradeStatus.FAILED),
                score_at_entry=before[-1] if before else None,
            )
        )
    return results


async def load_gaps(
    factory: Factory, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(FeedGap.started_at, FeedGap.ended_at).where(
                    FeedGap.started_at < end,
                    (FeedGap.ended_at.is_(None)) | (FeedGap.ended_at > start),
                )
            )
        ).all()
    return [(max(s, start), min(e or end, end)) for s, e in rows]


async def load_outcome_labels(
    factory: Factory, start: datetime, end: datetime
) -> list[OutcomeLabel]:
    """Outcome labels with the score band given by the first analysis of each token."""
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    TokenScore.features["band"].astext,
                    TokenOutcome.horizon_minutes,
                    TokenOutcome.max_return,
                    TokenOutcome.min_return,
                    TokenOutcome.last_return,
                    TokenOutcome.graduated,
                    TokenOutcome.gap_overlap,
                )
                .join(
                    TokenScore,
                    (TokenScore.token_id == TokenOutcome.token_id)
                    & (TokenScore.created_at == TokenOutcome.reference_at),
                )
                .where(TokenOutcome.reference_at >= start, TokenOutcome.reference_at < end)
            )
        ).all()
    return [
        OutcomeLabel(band or "unknown", h, mx, mn, last, bool(g), bool(gap))
        for band, h, mx, mn, last, g, gap in rows
    ]


def overlaps(start: datetime, end: datetime, gaps: list[tuple[datetime, datetime]]) -> bool:
    return any(gap_start < end and gap_end > start for gap_start, gap_end in gaps)


def sell_count(trades: list[Trade]) -> int:
    return sum(1 for t in trades if t.side is TradeSide.SELL)
