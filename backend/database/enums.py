"""Enumerations persisted in the database (stored as their string value, checked by constraint)."""

from enum import StrEnum


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ExecutionModeDb(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class TokenStatus(StrEnum):
    DISCOVERED = "discovered"
    TRACKING = "tracking"
    REJECTED = "rejected"
    GRADUATED = "graduated"
    DEAD = "dead"


class PoolKind(StrEnum):
    BONDING_CURVE = "bonding_curve"
    PUMPSWAP = "pumpswap"
    OTHER = "other"


class PoolStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    MIGRATED = "migrated"
    CLOSED = "closed"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    STUCK = "stuck"  # held tokens that currently cannot be sold


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TradeStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EXPIRED = "expired"
