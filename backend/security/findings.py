"""Accumulator for security findings and the resulting classification."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from backend.database.enums import RiskLevel

WARNING_PENALTY = Decimal(15)


@dataclass(slots=True)
class Findings:
    critical: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def add_critical(self, flag: str) -> None:
        if flag not in self.critical:
            self.critical.append(flag)

    def add_warning(self, flag: str) -> None:
        if flag not in self.warnings:
            self.warnings.append(flag)

    def add_missing(self, item: str) -> None:
        if item not in self.missing:
            self.missing.append(item)

    @property
    def security_score(self) -> Decimal:
        """100 minus a fixed penalty per warning; any critical flag forces 0."""
        if self.critical:
            return Decimal(0)
        return max(Decimal(0), Decimal(100) - WARNING_PENALTY * len(self.warnings))

    @property
    def risk_level(self) -> RiskLevel:
        if self.critical:
            return RiskLevel.CRITICAL
        if len(self.warnings) >= 3 or self.missing:
            return RiskLevel.HIGH
        if self.warnings:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW
