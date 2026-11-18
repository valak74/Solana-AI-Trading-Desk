"""Persistence of paper-trading activity (positions, trades, decisions, portfolio snapshots)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import (
    ExecutionModeDb,
    PositionStatus,
    TradeSide,
    TradeStatus,
)
from backend.database.models import (
    AgentDecision,
    ConfigVersion,
    PortfolioSnapshot,
    Position,
    Token,
    Trade,
)
from backend.database.session import transaction
from backend.trading.config import PaperTradingConfig
from backend.trading.positions import PaperPosition

INTERRUPTED = "interrupted_by_restart"


@dataclass(frozen=True, slots=True)
class TradeRecord:
    key: str
    side: TradeSide
    status: TradeStatus
    quantity: int
    sol_amount_lamports: int | None
    price: Decimal | None
    fees_lamports: int
    network_fee_lamports: int
    rent_lamports: int
    fill_model: str
    expected_out: int | None
    actual_out: int | None
    failure_reason: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class RestoredStrategy:
    realized: list[tuple[datetime, int]]
    entries: list[tuple[datetime, str]]
    realized_total: int
    interrupted_cost: int


class TradingStore(Protocol):
    async def config_version_id(self, config: PaperTradingConfig) -> int: ...

    async def open_position(self, pos: PaperPosition, config_id: int, run_id: str) -> int: ...

    async def record_trade(self, position_id: int, trade: TradeRecord) -> None: ...

    async def update_position(self, pos: PaperPosition) -> None: ...

    async def close_position(
        self, pos: PaperPosition, pnl: int | None, at: datetime, status: PositionStatus
    ) -> None: ...

    async def record_decision(
        self,
        agent: str,
        mint: str,
        decision: str,
        reason: str,
        payload: dict[str, Any],
        at: datetime,
        config_id: int,
    ) -> None: ...

    async def snapshot(
        self, run_id: str, strategy: str, at: datetime, values: dict[str, Any]
    ) -> None: ...

    async def restore(self, run_id: str, strategy: str, since: datetime) -> RestoredStrategy: ...


class SqlTradingStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def config_version_id(self, config: PaperTradingConfig) -> int:
        version_hash = config.version_hash()
        async with transaction(self._factory) as session:
            await session.execute(
                insert(ConfigVersion)
                .values(
                    strategy=config.name,
                    version_hash=version_hash,
                    params=config.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=["version_hash"])
            )
            found = await session.execute(
                select(ConfigVersion.id).where(ConfigVersion.version_hash == version_hash)
            )
            return int(found.scalar_one())

    async def _token_id(self, session: AsyncSession, mint: str) -> int:
        await session.execute(
            insert(Token)
            .values(mint_address=mint)
            .on_conflict_do_nothing(index_elements=["mint_address"])
        )
        found = await session.execute(select(Token.id).where(Token.mint_address == mint))
        return int(found.scalar_one())

    async def open_position(self, pos: PaperPosition, config_id: int, run_id: str) -> int:
        async with transaction(self._factory) as session:
            row = Position(
                token_id=await self._token_id(session, pos.mint),
                strategy=pos.strategy,
                run_id=run_id,
                config_version_id=config_id,
                execution_mode=ExecutionModeDb.PAPER,
                entry_price=pos.entry_price,
                quantity=pos.quantity,
                invested_lamports=pos.cost_lamports,
                remaining_quantity=pos.remaining,
                status=PositionStatus.OPEN if pos.remaining > 0 else PositionStatus.CLOSED,
                opened_at=pos.opened_at,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    async def record_trade(self, position_id: int, trade: TradeRecord) -> None:
        async with transaction(self._factory) as session:
            session.add(
                Trade(
                    position_id=position_id,
                    idempotency_key=trade.key,
                    side=trade.side,
                    execution_mode=ExecutionModeDb.PAPER,
                    status=trade.status,
                    quantity=trade.quantity,
                    price=trade.price,
                    sol_amount_lamports=trade.sol_amount_lamports,
                    expected_out=trade.expected_out,
                    actual_out=trade.actual_out,
                    fees_lamports=trade.fees_lamports,
                    network_fee_lamports=trade.network_fee_lamports,
                    rent_lamports=trade.rent_lamports,
                    fill_model=trade.fill_model,
                    failure_reason=trade.failure_reason,
                    created_at=trade.at,
                )
            )

    async def update_position(self, pos: PaperPosition) -> None:
        if pos.db_id is None:
            return
        async with transaction(self._factory) as session:
            await session.execute(
                update(Position)
                .where(Position.id == pos.db_id)
                .values(remaining_quantity=pos.remaining, invested_lamports=pos.cost_lamports)
            )

    async def close_position(
        self, pos: PaperPosition, pnl: int | None, at: datetime, status: PositionStatus
    ) -> None:
        if pos.db_id is None:
            return
        async with transaction(self._factory) as session:
            await session.execute(
                update(Position)
                .where(Position.id == pos.db_id)
                .values(
                    remaining_quantity=pos.remaining,
                    invested_lamports=pos.cost_lamports,
                    status=status,
                    exit_reason=pos.exit_reason,
                    realized_pnl_lamports=pnl,
                    closed_at=at,
                )
            )

    async def record_decision(
        self,
        agent: str,
        mint: str,
        decision: str,
        reason: str,
        payload: dict[str, Any],
        at: datetime,
        config_id: int,
    ) -> None:
        async with transaction(self._factory) as session:
            session.add(
                AgentDecision(
                    agent_name=agent,
                    strategy=payload.get("strategy"),
                    token_id=await self._token_id(session, mint),
                    config_version_id=config_id,
                    decision=decision,
                    reasoning_summary=reason[:1000],
                    structured_output=payload,
                    created_at=at,
                )
            )

    async def snapshot(
        self, run_id: str, strategy: str, at: datetime, values: dict[str, Any]
    ) -> None:
        async with transaction(self._factory) as session:
            session.add(
                PortfolioSnapshot(
                    timestamp=at,
                    execution_mode=ExecutionModeDb.PAPER,
                    strategy=strategy,
                    run_id=run_id,
                    **values,
                )
            )

    async def restore(self, run_id: str, strategy: str, since: datetime) -> RestoredStrategy:
        """Rebuild risk state after a restart. Positions left open are closed as interrupted.

        Their outcome is unknown (no data while the process was down): realised PnL stays NULL
        so they are excluded from performance statistics, but their cost is treated as spent for
        the virtual capital, the conservative choice.
        """
        async with transaction(self._factory) as session:
            base = (
                Position.run_id == run_id,
                Position.strategy == strategy,
                Position.execution_mode == ExecutionModeDb.PAPER,
            )
            open_rows = (
                (
                    await session.execute(
                        select(Position).where(*base, Position.status == PositionStatus.OPEN)
                    )
                )
                .scalars()
                .all()
            )
            for row in open_rows:
                row.status = PositionStatus.CLOSED
                row.exit_reason = INTERRUPTED
                row.closed_at = since if row.opened_at < since else row.opened_at
            await session.flush()
            closed = (
                await session.execute(
                    select(
                        Position.closed_at,
                        Position.realized_pnl_lamports,
                        Position.exit_reason,
                        Position.invested_lamports,
                    ).where(*base, Position.status == PositionStatus.CLOSED)
                )
            ).all()
            entries = (
                await session.execute(
                    select(Position.opened_at, Token.mint_address)
                    .join(Token, Token.id == Position.token_id)
                    .where(*base, Position.opened_at >= since - timedelta(days=7))
                )
            ).all()
        realized = [(at, pnl) for at, pnl, _, _ in closed if at is not None and pnl is not None]
        interrupted_cost = sum(
            cost for _, pnl, reason, cost in closed if pnl is None and reason == INTERRUPTED
        )
        return RestoredStrategy(
            realized=[r for r in realized if r[0] >= since - timedelta(days=7)],
            entries=[(at, mint) for at, mint in entries],
            realized_total=sum(pnl for _, pnl in realized),
            interrupted_cost=interrupted_cost,
        )
