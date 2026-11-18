import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import RiskLevel, Severity
from backend.database.models import Position, SecurityAnalysis, SystemEvent, Token, TokenScore
from backend.database.session import transaction
from backend.reporting.report import ReportConfig
from backend.reporting.service import WeeklySummary, generate_report
from backend.scanner.feed_monitor import FeedMonitor
from backend.scanner.processor import IngestProcessor
from backend.scanner.store import IngestStore
from backend.scanner.tracking import TrackingRegistry
from backend.trading.config import ExecutionModel, PaperTradingConfig, StrategyConfig
from backend.trading.replay import decision_from, load_config, load_signals, replay
from tests.fixtures.pumpportal import mint
from tests.fixtures.trading import created, seconds, traded

Factory = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TOKEN, CREATOR = mint(1), mint(9)


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, severity: Severity, title: str, body: str = "") -> bool:
        self.sent.append(body)
        return True


async def record_market(factory: Factory) -> None:
    """A token created at T0 whose price doubles after the analysis at T0 + 3 min."""
    reg = TrackingRegistry(timedelta(hours=2), timedelta(hours=6), 0, timedelta(hours=1), 100)
    p = IngestProcessor(reg, FeedMonitor(timedelta(minutes=10)), rng=random.Random(0))
    p.handle(created(TOKEN, T0, CREATOR))
    path = [
        (60, 1.0),
        (170, 1.0),
        (182, 1.0),
        (240, 1.5),
        (300, 2.0),
        (360, 2.4),
        (420, 1.7),
        (480, 1.7),
        (2000, 1.7),
    ]
    for i, (s, multiple) in enumerate(path):
        p.handle(traded(TOKEN, seconds(T0, s), multiple, seq=i))
    await IngestStore(factory).write(p.take_batch())
    async with transaction(factory) as session:
        token_id = (await session.execute(select(Token.id))).scalar_one()
        at = seconds(T0, 180)
        session.add(
            SecurityAnalysis(
                token_id=token_id,
                security_score=Decimal(100),
                risk_level=RiskLevel.LOW,
                metrics={
                    "curve_tokens_sold_share": "0.40",
                    "unique_buyers": 55,
                    "entry_price_impact": "0.02",
                },
                created_at=at,
            )
        )
        session.add(
            TokenScore(
                token_id=token_id,
                liquidity_score=Decimal(90),
                momentum_score=Decimal(90),
                wallet_score=Decimal(90),
                holder_score=Decimal(90),
                volume_score=Decimal(90),
                total_score=Decimal(90),
                features={"band": "priority", "deep_analysis": True},
                created_at=at,
            )
        )


def config() -> PaperTradingConfig:
    return PaperTradingConfig(
        strategies=(StrategyConfig(name="A"),),
        execution=ExecutionModel(buy_failure_rate=Decimal(0), sell_failure_rate=Decimal(0)),
    )


def test_decision_reconstruction() -> None:
    assert decision_from(["mint_authority_active"], {"band": "priority"}) == "reject"
    assert decision_from([], {"band": "priority", "deep_analysis": False}) == "watch"
    assert decision_from([], {"band": "candidate", "deep_analysis": True}) == "candidate"


async def test_signals_are_rebuilt_from_stored_analyses(session_factory: Factory) -> None:
    await record_market(session_factory)
    (signal,) = await load_signals(session_factory, T0, T0 + timedelta(hours=1))
    assert signal.mint == TOKEN and signal.decision == "priority"
    assert signal.entry_impact == Decimal("0.02")


async def test_replay_trades_the_recorded_market_and_is_reported(session_factory: Factory) -> None:
    await record_market(session_factory)
    end = T0 + timedelta(hours=1)
    result = await replay(
        session_factory, config(), run_id="replay-test", start=T0, end=end, include_holdout=True
    )
    assert result.events == 10 and result.analyses == 1
    assert result.counters["entries"] == 1 and result.counters["exits"] == 1
    async with session_factory() as session:
        position = (await session.execute(select(Position))).scalar_one()
    assert position.run_id == "replay-test" and position.exit_reason == "trailing_stop"
    assert position.realized_pnl_lamports is not None and position.realized_pnl_lamports > 0

    report = await generate_report(
        session_factory, ReportConfig(resamples=200), run_id="replay-test", start=T0, end=end
    )
    assert report is not None
    a = next(s for s in report.strategies if s.name == "A")
    assert a.overall.attempts == 1 and a.overall.total_pnl == position.realized_pnl_lamports
    assert a.verdict == "DONNÉES INSUFFISANTES"
    # Same data, same parameters: the replay is reproducible.
    again = await replay(
        session_factory, config(), run_id="replay-test-2", start=T0, end=end, include_holdout=True
    )
    async with session_factory() as session:
        pnls = (
            await session.execute(
                select(Position.run_id, Position.realized_pnl_lamports).order_by(Position.run_id)
            )
        ).all()
    assert again.counters == result.counters
    assert pnls[0][1] == pnls[1][1]


async def test_replay_stops_before_the_reserved_period(session_factory: Factory) -> None:
    await record_market(session_factory)
    result = await replay(
        session_factory,
        config(),
        run_id="replay-learning",
        start=T0,
        end=T0 + timedelta(minutes=10),
    )
    assert not result.included_holdout
    assert result.end == T0 + timedelta(minutes=7)  # 70 % of 10 minutes
    assert result.events < 10


def test_partial_config_override(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "variant.json"
    path.write_text('{"risk": {"max_open_positions": 1}, "seed": 5}', encoding="utf-8")
    variant = load_config(str(path))
    assert variant.risk.max_open_positions == 1 and variant.seed == 5
    assert variant.risk.max_position_lamports == PaperTradingConfig().risk.max_position_lamports
    assert variant.version_hash() != PaperTradingConfig().version_hash()


async def test_weekly_summary_is_sent_once_per_week(session_factory: Factory) -> None:
    await record_market(session_factory)
    now = [T0 + timedelta(days=8)]
    alerter = RecordingAlerter()
    weekly = WeeklySummary(
        session_factory, alerter, ReportConfig(resamples=100), clock=lambda: now[0]
    )
    assert await weekly.run_once()
    assert not await weekly.run_once()  # already sent this week
    assert "Simulation" in alerter.sent[0]
    async with session_factory() as session:
        sent = (
            await session.execute(
                select(func.count())
                .select_from(SystemEvent)
                .where(SystemEvent.event_type == "weekly_report_sent")
            )
        ).scalar_one()
    assert sent == 1


async def test_report_without_data_returns_none(session_factory: Factory) -> None:
    assert await generate_report(session_factory, ReportConfig()) is None
