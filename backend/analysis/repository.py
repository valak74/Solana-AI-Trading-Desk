"""Persistence and candidate selection for token analyses."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.analysis.engine import AnalysisResult, AnalysisTarget
from backend.database.enums import PoolKind, TokenStatus
from backend.database.models import (
    AgentDecision,
    ConfigVersion,
    Pool,
    RawEvent,
    SecurityAnalysis,
    Token,
    TokenScore,
)
from backend.database.session import transaction
from backend.security.config import AnalysisConfig


def _d(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal(0)


class AnalysisRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def config_version_id(self, config: AnalysisConfig) -> int:
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

    async def candidates(
        self,
        now: datetime,
        min_age: timedelta,
        max_age: timedelta,
        reanalyze_after: timedelta,
        min_trades: int,
        limit: int,
    ) -> list[AnalysisTarget]:
        trade_count = (
            select(func.count())
            .select_from(RawEvent)
            .where(
                RawEvent.event_type == "trade",
                RawEvent.payload["mint"].astext == Token.mint_address,
                RawEvent.received_at <= now,
            )
            .scalar_subquery()
        )
        recently_analyzed = exists().where(
            SecurityAnalysis.token_id == Token.id,
            SecurityAnalysis.created_at > now - reanalyze_after,
        )
        query = (
            select(
                Token.id,
                Token.mint_address,
                Token.creator_wallet,
                Token.first_seen_at,
                Pool.address,
            )
            .join(Pool, and_(Pool.token_id == Token.id, Pool.kind == PoolKind.BONDING_CURVE))
            .where(
                Token.first_seen_at <= now - min_age,
                Token.first_seen_at >= now - max_age,
                Token.status.in_([TokenStatus.DISCOVERED, TokenStatus.TRACKING]),
                ~recently_analyzed,
                trade_count >= min_trades,
            )
            .order_by(trade_count.desc())
            .limit(limit)
        )
        async with self._factory() as session:
            rows = (await session.execute(query)).all()
        return [AnalysisTarget(r[0], r[1], r[2], r[4], r[3]) for r in rows]

    async def creator_launches(self, creator: str | None, now: datetime) -> int:
        if creator is None:
            return 0
        async with self._factory() as session:
            count = await session.execute(
                select(func.count())
                .select_from(Token)
                .where(
                    Token.creator_wallet == creator,
                    Token.first_seen_at > now - timedelta(hours=24),
                    Token.first_seen_at <= now,
                )
            )
            return int(count.scalar_one())

    async def save(
        self,
        target: AnalysisTarget,
        result: AnalysisResult,
        config_version_id: int,
        duration_ms: int,
    ) -> None:
        f, final = result.findings, result.final
        c = final.components
        security_output: dict[str, Any] = {
            "critical_flags": f.critical,
            "warnings": f.warnings,
            "missing_data": f.missing,
            "prescore": str(result.prescore.total),
        }
        async with transaction(self._factory) as session:
            session.add(
                SecurityAnalysis(
                    token_id=target.token_id,
                    config_version_id=config_version_id,
                    security_score=f.security_score,
                    risk_level=f.risk_level,
                    critical_flags=f.critical,
                    warnings=f.warnings,
                    missing_data=f.missing,
                    metrics=f.metrics,
                    created_at=result.analyzed_at,
                )
            )
            session.add(
                TokenScore(
                    token_id=target.token_id,
                    config_version_id=config_version_id,
                    liquidity_score=_d(c["liquidity"]),
                    momentum_score=_d(c["momentum"]),
                    wallet_score=_d(c["wallet"]),
                    holder_score=_d(c["holder"]),
                    volume_score=_d(c["volume"]),
                    sentiment_score=c["sentiment"],
                    total_score=final.total,
                    features=final.as_features() | {"deep_analysis": result.deep_done},
                    created_at=result.analyzed_at,
                )
            )
            session.add(
                AgentDecision(
                    agent_name="security_analyst",
                    token_id=target.token_id,
                    config_version_id=config_version_id,
                    decision="reject" if f.critical else "pass",
                    reasoning_summary=", ".join(f.critical or f.warnings)[:1000] or None,
                    structured_output=security_output,
                    duration_ms=duration_ms,
                    created_at=result.analyzed_at,
                )
            )
            session.add(
                AgentDecision(
                    agent_name="scorer",
                    token_id=target.token_id,
                    config_version_id=config_version_id,
                    decision=result.decision,
                    reasoning_summary=f"score={final.total} band={final.band}",
                    structured_output=final.as_features() | {"total": str(final.total)},
                    duration_ms=duration_ms,
                    created_at=result.analyzed_at,
                )
            )
