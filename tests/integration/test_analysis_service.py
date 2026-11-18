import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.analysis.engine import TokenAnalyzer
from backend.analysis.flows import load_trades
from backend.analysis.repository import AnalysisRepository
from backend.analysis.service import AnalysisService
from backend.database.enums import Severity
from backend.database.models import (
    AgentDecision,
    ConfigVersion,
    SecurityAnalysis,
    SystemEvent,
    TokenScore,
)
from backend.integrations.helius import CreditBudget
from backend.scanner.feed_monitor import FeedMonitor
from backend.scanner.processor import IngestProcessor
from backend.scanner.providers.base import RawMessage
from backend.scanner.store import IngestStore
from backend.scanner.tracking import TrackingRegistry
from backend.security.config import AnalysisConfig
from tests.fixtures.chain import FakeReader, curve_account, mint_account
from tests.fixtures.pumpportal import b58, create_msg, mint, sig, trade_msg

Factory = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
CURVE = b58(44, 777)  # bondingCurveKey used by the message fixtures


class RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str]] = []

    async def send(self, severity: Severity, title: str, body: str = "") -> bool:
        self.sent.append((severity, title))
        return True


async def record_token(factory: Factory, token: str, n_trades: int, *, seed: int = 0) -> None:
    """Simulate the scanner: a creation at T0 followed by ``n_trades`` buys by distinct wallets."""
    reg = TrackingRegistry(timedelta(hours=1), timedelta(hours=6), 0, timedelta(hours=1), 100)
    p = IngestProcessor(reg, FeedMonitor(timedelta(seconds=60)), rng=random.Random(0))
    p.handle(RawMessage("pumpportal", T0, create_msg(token, sig(seed)), 100))
    for i in range(n_trades):
        payload = trade_msg(token, sig(seed + 1 + i), traderPublicKey=b58(44, 5000 + seed + i))
        p.handle(RawMessage("pumpportal", T0 + timedelta(seconds=10 + i), payload, 100))
    await IngestStore(factory).write(p.take_batch())


def chain_for(token: str) -> FakeReader:
    reader = FakeReader()
    reader.accounts[token] = mint_account()
    reader.accounts[CURVE] = curve_account()
    return reader


def service(
    factory: Factory,
    reader: FakeReader,
    now: datetime,
    budget: CreditBudget,
    alerter: RecordingAlerter,
) -> AnalysisService:
    config = AnalysisConfig()
    return AnalysisService(
        TokenAnalyzer(reader, config, clock=lambda: now),
        AnalysisRepository(factory),
        factory,
        config,
        budget,
        alerter,
        min_trades=15,
        clock=lambda: now,
    )


async def count(factory: Factory, model: type[object]) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_load_trades_never_reads_the_future(session_factory: Factory) -> None:
    await record_token(session_factory, mint(1), 20)
    trades = await load_trades(session_factory, mint(1), T0 + timedelta(seconds=14))
    assert len(trades) == 5  # trades at +10s .. +14s only


async def test_analysis_cycle_persists_traceable_results(session_factory: Factory) -> None:
    await record_token(session_factory, mint(1), 20)
    now = T0 + timedelta(minutes=5)
    budget, alerter = CreditBudget(10_000), RecordingAlerter()
    svc = service(session_factory, chain_for(mint(1)), now, budget, alerter)

    assert await svc.run_once() == 1
    assert await svc.run_once() == 0  # already analysed within the re-analysis window

    async with session_factory() as session:
        analysis = (await session.execute(select(SecurityAnalysis))).scalar_one()
        token_score = (await session.execute(select(TokenScore))).scalar_one()
        decisions = (await session.execute(select(AgentDecision.agent_name))).scalars().all()
        config = (await session.execute(select(ConfigVersion))).scalar_one()
    assert analysis.created_at == now
    assert analysis.config_version_id == config.id == token_score.config_version_id
    assert analysis.metrics["n_buys"] == 20
    assert "token_age_seconds" in analysis.metrics
    assert token_score.features["band"] in {"reject", "watch", "candidate", "priority"}
    assert sorted(decisions) == ["scorer", "security_analyst"]
    assert config.params["name"] == "analysis-v1"


async def test_candidate_selection_respects_age_and_activity(session_factory: Factory) -> None:
    await record_token(session_factory, mint(1), 20, seed=0)
    await record_token(session_factory, mint(2), 3, seed=100)  # too little activity
    budget, alerter = CreditBudget(10_000), RecordingAlerter()
    too_young = service(
        session_factory, chain_for(mint(1)), T0 + timedelta(minutes=1), budget, alerter
    )
    assert await too_young.run_once() == 0
    too_old = service(session_factory, chain_for(mint(1)), T0 + timedelta(hours=1), budget, alerter)
    assert await too_old.run_once() == 0
    in_window = service(
        session_factory, chain_for(mint(1)), T0 + timedelta(minutes=4), budget, alerter
    )
    assert await in_window.run_once() == 1
    assert await count(session_factory, SecurityAnalysis) == 1


async def test_budget_exhaustion_is_reported_once(session_factory: Factory) -> None:
    await record_token(session_factory, mint(1), 20)
    budget, alerter = CreditBudget(10), RecordingAlerter()  # below the per-analysis reserve
    svc = service(session_factory, chain_for(mint(1)), T0 + timedelta(minutes=5), budget, alerter)
    assert await svc.run_once() == 0
    assert await svc.run_once() == 0
    assert alerter.sent == [(Severity.WARNING, "Budget de crédits Helius atteint")]
    async with session_factory() as session:
        events = (
            (
                await session.execute(
                    select(SystemEvent.event_type).where(
                        SystemEvent.event_type == "analysis_budget_exhausted"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert events == ["analysis_budget_exhausted"]
