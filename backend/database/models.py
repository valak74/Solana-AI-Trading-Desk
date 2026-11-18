"""ORM models for the measurement stage.

Units are part of column names (``*_lamports``, raw token amounts in ``quantity``) so that no
amount is ever ambiguous. All timestamps are timezone-aware (UTC).
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.database.enums import (
    ExecutionModeDb,
    PoolKind,
    PoolStatus,
    PositionStatus,
    RiskLevel,
    Severity,
    TokenStatus,
    TradeSide,
    TradeStatus,
)
from backend.database.types import PRICE, RATIO, SCORE, U64, str_enum

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

PUBKEY = String(44)  # base58 Solana public key
SIGNATURE = String(88)  # base58 transaction signature


def _utcnow_column(*, nullable: bool = False) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=nullable)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


class ConfigVersion(Base):
    """Immutable snapshot of a strategy's parameters; every decision references one."""

    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    version_hash: Mapped[str] = mapped_column(String(64), unique=True)
    params: Mapped[dict[str, Any]]
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _utcnow_column()


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mint_address: Mapped[str] = mapped_column(PUBKEY, unique=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(256))
    decimals: Mapped[int | None] = mapped_column(SmallInteger)
    program_id: Mapped[str | None] = mapped_column(PUBKEY)  # SPL Token or Token-2022
    creator_wallet: Mapped[str | None] = mapped_column(PUBKEY, index=True)
    launchpad: Mapped[str | None] = mapped_column(String(32))
    metadata_uri: Mapped[str | None] = mapped_column(Text)
    created_slot: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # on-chain time
    first_seen_at: Mapped[datetime] = _utcnow_column()  # our detection time
    status: Mapped[TokenStatus] = mapped_column(
        str_enum(TokenStatus, "token_status"), default=TokenStatus.DISCOVERED
    )

    __table_args__ = (
        CheckConstraint("decimals IS NULL OR (decimals >= 0 AND decimals <= 18)", name="decimals"),
    )


class Pool(Base):
    __tablename__ = "pools"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    address: Mapped[str] = mapped_column(PUBKEY, unique=True)
    program_id: Mapped[str] = mapped_column(PUBKEY)
    kind: Mapped[PoolKind] = mapped_column(str_enum(PoolKind, "pool_kind"))
    status: Mapped[PoolStatus] = mapped_column(
        str_enum(PoolStatus, "pool_status"), default=PoolStatus.ACTIVE
    )
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = _utcnow_column()


class RawEvent(Base):
    """Append-only record of every event received from a data provider (basis for replay)."""

    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(String(256), unique=True)
    slot: Mapped[int | None] = mapped_column(BigInteger)
    signature: Mapped[str | None] = mapped_column(SIGNATURE, index=True)
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]]

    __table_args__ = (
        Index("ix_raw_events_type_received", "event_type", "received_at"),
        Index("ix_raw_events_received_at", "received_at"),
    )


class TokenSnapshot(Base):
    """Point-in-time market state of a token (prices in SOL per whole token)."""

    __tablename__ = "token_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot: Mapped[int | None] = mapped_column(BigInteger)
    price_sol: Mapped[Decimal | None] = mapped_column(PRICE)
    liquidity_lamports: Mapped[int | None] = mapped_column(BigInteger)
    volume_lamports: Mapped[int | None] = mapped_column(BigInteger)
    virtual_sol_reserves: Mapped[int | None] = mapped_column(BigInteger)
    virtual_token_reserves: Mapped[int | None] = mapped_column(U64)
    curve_progress: Mapped[Decimal | None] = mapped_column(RATIO)  # 0..1
    holders_count: Mapped[int | None] = mapped_column(Integer)
    buy_count: Mapped[int | None] = mapped_column(Integer)
    sell_count: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_token_snapshots_token_time", "token_id", "timestamp"),
        CheckConstraint("price_sol IS NULL OR price_sol >= 0", name="price_non_negative"),
        CheckConstraint(
            "curve_progress IS NULL OR (curve_progress >= 0 AND curve_progress <= 1)",
            name="curve_progress_range",
        ),
    )


class SecurityAnalysis(Base):
    __tablename__ = "security_analyses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    config_version_id: Mapped[int | None] = mapped_column(ForeignKey("config_versions.id"))
    security_score: Mapped[Decimal] = mapped_column(SCORE)
    risk_level: Mapped[RiskLevel] = mapped_column(str_enum(RiskLevel, "risk_level"))
    critical_flags: Mapped[list[Any]] = mapped_column(default=list)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    missing_data: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _utcnow_column()

    __table_args__ = (
        CheckConstraint("security_score >= 0 AND security_score <= 100", name="score_range"),
    )


class TokenScore(Base):
    __tablename__ = "token_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    config_version_id: Mapped[int | None] = mapped_column(ForeignKey("config_versions.id"))
    liquidity_score: Mapped[Decimal] = mapped_column(SCORE)
    momentum_score: Mapped[Decimal] = mapped_column(SCORE)
    wallet_score: Mapped[Decimal] = mapped_column(SCORE)
    holder_score: Mapped[Decimal] = mapped_column(SCORE)
    volume_score: Mapped[Decimal] = mapped_column(SCORE)
    sentiment_score: Mapped[Decimal | None] = mapped_column(SCORE)  # absent while measuring
    total_score: Mapped[Decimal] = mapped_column(SCORE)
    created_at: Mapped[datetime] = _utcnow_column()

    __table_args__ = (
        CheckConstraint("total_score >= 0 AND total_score <= 100", name="total_range"),
    )


class AgentDecision(Base):
    __tablename__ = "agent_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str | None] = mapped_column(String(64))
    token_id: Mapped[int | None] = mapped_column(ForeignKey("tokens.id"), index=True)
    config_version_id: Mapped[int | None] = mapped_column(ForeignKey("config_versions.id"))
    decision: Mapped[str] = mapped_column(String(32))
    reasoning_summary: Mapped[str | None] = mapped_column(Text)
    structured_output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _utcnow_column()


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    config_version_id: Mapped[int | None] = mapped_column(ForeignKey("config_versions.id"))
    execution_mode: Mapped[ExecutionModeDb] = mapped_column(
        str_enum(ExecutionModeDb, "position_execution_mode")
    )
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    quantity: Mapped[int] = mapped_column(U64)  # raw token units actually received
    invested_lamports: Mapped[int] = mapped_column(BigInteger)  # all-in cost, fees included
    remaining_quantity: Mapped[int] = mapped_column(U64)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[PositionStatus] = mapped_column(
        str_enum(PositionStatus, "position_status"), default=PositionStatus.OPEN
    )
    exit_reason: Mapped[str | None] = mapped_column(String(64))
    realized_pnl_lamports: Mapped[int | None] = mapped_column(BigInteger)
    opened_at: Mapped[datetime] = _utcnow_column()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("invested_lamports >= 0", name="invested_non_negative"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("remaining_quantity >= 0", name="remaining_non_negative"),
        CheckConstraint("remaining_quantity <= quantity", name="remaining_le_quantity"),
        Index("ix_positions_strategy_status", "strategy", "status"),
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    side: Mapped[TradeSide] = mapped_column(str_enum(TradeSide, "trade_side"))
    execution_mode: Mapped[ExecutionModeDb] = mapped_column(
        str_enum(ExecutionModeDb, "trade_execution_mode")
    )
    status: Mapped[TradeStatus] = mapped_column(
        str_enum(TradeStatus, "trade_status"), default=TradeStatus.PENDING
    )
    quantity: Mapped[int] = mapped_column(U64)  # raw token units
    price: Mapped[Decimal | None] = mapped_column(PRICE)  # effective SOL per whole token
    sol_amount_lamports: Mapped[int | None] = mapped_column(BigInteger)
    expected_out: Mapped[int | None] = mapped_column(U64)
    actual_out: Mapped[int | None] = mapped_column(U64)
    fees_lamports: Mapped[int] = mapped_column(BigInteger, default=0)  # protocol + aggregator
    network_fee_lamports: Mapped[int] = mapped_column(BigInteger, default=0)  # base+priority+tip
    rent_lamports: Mapped[int] = mapped_column(BigInteger, default=0)  # >0 paid, <0 refunded
    slippage_bps: Mapped[Decimal | None] = mapped_column(RATIO)
    transaction_signature: Mapped[str | None] = mapped_column(SIGNATURE, unique=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _utcnow_column()

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("fees_lamports >= 0", name="fees_non_negative"),
        CheckConstraint("network_fee_lamports >= 0", name="network_fee_non_negative"),
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    execution_mode: Mapped[ExecutionModeDb] = mapped_column(
        str_enum(ExecutionModeDb, "portfolio_execution_mode")
    )
    strategy: Mapped[str | None] = mapped_column(String(64))  # NULL = all strategies
    total_equity_lamports: Mapped[int] = mapped_column(BigInteger)
    available_lamports: Mapped[int] = mapped_column(BigInteger)
    reserved_lamports: Mapped[int] = mapped_column(BigInteger)
    realized_pnl_lamports: Mapped[int] = mapped_column(BigInteger)
    unrealized_pnl_lamports: Mapped[int] = mapped_column(BigInteger)
    drawdown: Mapped[Decimal] = mapped_column(RATIO)  # fraction of peak equity, 0..1

    __table_args__ = (Index("ix_portfolio_snapshots_mode_time", "execution_mode", "timestamp"),)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "severity"))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _utcnow_column()

    __table_args__ = (Index("ix_system_events_type_created", "event_type", "created_at"),)


class FeedGap(Base):
    """Interval without data from a provider. Excluded from strategy evaluation."""

    __tablename__ = "feed_gaps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_feed_gaps_source_started", "source", "started_at"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ordered"),
    )


class VerificationSample(Base):
    """On-chain cross-check of a sampled provider event (existence, success, latency)."""

    __tablename__ = "verification_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(SIGNATURE, index=True)
    mint: Mapped[str] = mapped_column(PUBKEY)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    found: Mapped[bool] = mapped_column(Boolean)
    tx_succeeded: Mapped[bool | None] = mapped_column(Boolean)
    mint_present: Mapped[bool | None] = mapped_column(Boolean)
    slot: Mapped[int | None] = mapped_column(BigInteger)
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detection_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(String(256))
