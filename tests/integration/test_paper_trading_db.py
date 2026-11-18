import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.analysis.outcomes import OutcomeLabeler
from backend.database.enums import (
    PositionStatus,
    RiskLevel,
    TokenStatus,
)
from backend.database.models import (
    AgentDecision,
    FeedGap,
    PortfolioSnapshot,
    Position,
    SecurityAnalysis,
    Token,
    TokenOutcome,
    TokenSnapshot,
    Trade,
)
from backend.database.session import transaction
from backend.execution.paper import PaperExecutor
from backend.risk import kill_switch
from backend.risk.manager import RiskManager
from backend.trading.config import ExecutionModel, PaperTradingConfig, StrategyConfig
from backend.trading.engine import PaperTradingEngine
from backend.trading.repository import SqlTradingStore
from tests.fixtures.pumpportal import mint
from tests.fixtures.trading import a_signal, created, seconds, traded

Factory = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
TOKEN, CREATOR = mint(1), mint(9)


def build(factory: Factory, kill: bool = False) -> PaperTradingEngine:
    config = PaperTradingConfig(
        strategies=(StrategyConfig(name="A"),),
        execution=ExecutionModel(buy_failure_rate=Decimal(0), sell_failure_rate=Decimal(0)),
    )
    risk = RiskManager(config.risk, lambda: kill, lambda _now: True)
    return PaperTradingEngine(
        config, PaperExecutor(config.execution, random.Random(1)), risk, SqlTradingStore(factory)
    )


async def test_paper_position_lifecycle_is_persisted(session_factory: Factory) -> None:
    eng = build(session_factory)
    await eng.start(T0)
    await eng.on_raw(created(TOKEN, T0, CREATOR))
    await eng.on_analysis(a_signal(TOKEN, seconds(T0, 180)))
    await eng.on_raw(traded(TOKEN, seconds(T0, 182), 1.0, seq=1))
    await eng.on_raw(traded(TOKEN, seconds(T0, 200), 0.8, seq=2))  # stop loss
    await eng.on_raw(traded(TOKEN, seconds(T0, 205), 0.8, seq=3))
    await eng.tick(seconds(T0, 206))

    async with session_factory() as session:
        position = (await session.execute(select(Position))).scalar_one()
        trades = (await session.execute(select(Trade).order_by(Trade.id))).scalars().all()
        agents = (await session.execute(select(AgentDecision.agent_name))).scalars().all()
        snapshot = (await session.execute(select(PortfolioSnapshot))).scalar_one()
    assert position.status is PositionStatus.CLOSED and position.exit_reason == "stop_loss"
    assert position.run_id == "live" and position.remaining_quantity == 0
    assert [t.side.value for t in trades] == ["buy", "sell"]
    assert trades[0].rent_lamports == 2_039_280 and trades[1].rent_lamports == -2_039_280
    assert trades[0].fill_model == "curve" and trades[0].expected_out is not None
    total_in = sum(
        (t.sol_amount_lamports or 0) - t.network_fee_lamports - t.rent_lamports
        for t in trades
        if t.side.value == "sell"
    )
    assert position.realized_pnl_lamports == total_in - position.invested_lamports
    assert sorted(set(agents)) == ["risk_manager", "strategist"]
    assert snapshot.strategy == "A" and snapshot.realized_pnl_lamports < 0


async def test_restart_closes_open_positions_as_interrupted(session_factory: Factory) -> None:
    eng = build(session_factory)
    await eng.start(T0)
    await eng.on_raw(created(TOKEN, T0, CREATOR))
    await eng.on_analysis(a_signal(TOKEN, seconds(T0, 180)))
    await eng.on_raw(traded(TOKEN, seconds(T0, 182), 1.0, seq=1))
    cost = eng.positions["A"][TOKEN].cost_lamports

    restarted = build(session_factory)
    await restarted.start(seconds(T0, 600))
    async with session_factory() as session:
        position = (await session.execute(select(Position))).scalar_one()
    assert position.exit_reason == "interrupted_by_restart"
    assert position.realized_pnl_lamports is None  # unknown outcome: excluded from statistics
    book = restarted.books["A"]
    assert book.cash == restarted.risk.limits.trading_capital_lamports - cost
    assert book.last_entry_by_token[TOKEN] == seconds(T0, 181.5)


async def test_kill_switch_persists_and_blocks(session_factory: Factory) -> None:
    assert not await kill_switch.is_active(session_factory)
    await kill_switch.set_state(session_factory, True, "test")
    assert await kill_switch.is_active(session_factory)
    await kill_switch.set_state(session_factory, False, None)
    assert not await kill_switch.is_active(session_factory)


async def test_unreadable_kill_switch_counts_as_active() -> None:
    from backend.database.session import create_engine, create_session_factory

    dead = create_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    try:
        assert await kill_switch.is_active(create_session_factory(dead))
    finally:
        await dead.dispose()


async def test_outcome_labels(session_factory: Factory) -> None:
    async with transaction(session_factory) as session:
        token = Token(mint_address=TOKEN, status=TokenStatus.DISCOVERED, first_seen_at=T0)
        session.add(token)
        await session.flush()
        ref = T0 + timedelta(minutes=5)
        session.add(
            SecurityAnalysis(
                token_id=token.id,
                security_score=Decimal(100),
                risk_level=RiskLevel.LOW,
                created_at=ref,
            )
        )
        for minute, price in [(4, "1.0"), (6, "1.5"), (10, "0.7"), (14, "1.2"), (40, "3.0")]:
            session.add(
                TokenSnapshot(
                    token_id=token.id,
                    timestamp=T0 + timedelta(minutes=minute),
                    price_sol=Decimal(price),
                )
            )
        session.add(
            FeedGap(
                source="pumpportal",
                reason="stale",
                started_at=T0 + timedelta(minutes=50),
                ended_at=T0 + timedelta(minutes=55),
            )
        )
    labeler = OutcomeLabeler(session_factory, clock=lambda: T0 + timedelta(hours=2))
    assert await labeler.run_once() == 4
    assert await labeler.run_once() == 0  # idempotent
    async with session_factory() as session:
        rows = {
            o.horizon_minutes: o
            for o in (await session.execute(select(TokenOutcome))).scalars().all()
        }
    assert rows[5].reference_price == Decimal("1.0")
    # Window (min 5, min 10]: prices 1.5 then 0.7 (the bound is inclusive).
    assert (rows[5].max_return, rows[5].min_return) == (Decimal("0.5"), Decimal("-0.3"))
    assert rows[5].snapshots == 2
    assert rows[15].min_return == Decimal("-0.3") and rows[15].last_return == Decimal("0.2")
    assert rows[30].gap_overlap is False and rows[60].gap_overlap is True
    assert rows[60].max_return == Decimal("2.0")
