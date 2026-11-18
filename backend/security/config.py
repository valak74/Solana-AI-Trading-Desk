"""Versioned analysis parameters.

Every threshold below is a starting hypothesis to be calibrated with the measurement data
(step M5), never a proven value. Each analysis references the hash of the exact parameters
used, so results produced under different settings are never mixed up.
"""

import hashlib
import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SecurityThresholds(BaseModel):
    model_config = ConfigDict(frozen=True)

    transfer_fee_warning_bps: int = 1
    transfer_fee_critical_bps: int = 100
    creator_share_warning: Decimal = Decimal("0.05")
    creator_share_critical: Decimal = Decimal("0.20")
    top10_share_warning: Decimal = Decimal("0.30")
    top10_share_critical: Decimal = Decimal("0.50")
    top1_share_critical: Decimal = Decimal("0.20")
    bundle_tx_warning: int = 4
    bundle_tx_critical: int = 8
    funding_cluster_critical: int = 3
    fresh_wallet_max_txs: int = 20
    fresh_wallet_max_age_hours: int = 24
    serial_creator_warning_24h: int = 3
    serial_creator_critical_24h: int = 10
    entry_impact_warning: Decimal = Decimal("0.05")
    rugcheck_danger_is_critical: bool = False


class ScoringWeights(BaseModel):
    """Spec weights. Sentiment is excluded (not renormalised to zero) while it is absent."""

    model_config = ConfigDict(frozen=True)

    liquidity: int = 20
    momentum: int = 20
    wallet: int = 20
    holder: int = 15
    volume: int = 15
    sentiment: int = 10


class ScoringBounds(BaseModel):
    """Linear ramps: value at ``*_zero`` scores 0, value at ``*_full`` scores 100."""

    model_config = ConfigDict(frozen=True)

    impact_full: Decimal = Decimal("0.01")
    impact_zero: Decimal = Decimal("0.05")
    buy_ratio_zero: Decimal = Decimal("0.50")
    buy_ratio_full: Decimal = Decimal("0.80")
    acceleration_zero: Decimal = Decimal("1.0")
    acceleration_full: Decimal = Decimal("2.0")
    top10_full: Decimal = Decimal("0.15")
    top10_zero: Decimal = Decimal("0.40")
    creator_full: Decimal = Decimal("0")
    creator_zero: Decimal = Decimal("0.10")
    unique_buyers_zero: int = 5
    unique_buyers_full: int = 40
    wash_full: Decimal = Decimal("0.10")
    wash_zero: Decimal = Decimal("0.50")
    recent_window_seconds: int = 120
    previous_window_seconds: int = 180


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = "analysis-v1"
    trade_size_lamports: int = Field(default=70_000_000, gt=0)  # simulated position (~0.07 SOL)
    curve_fee_bps: int = Field(default=125, ge=0, le=1000)
    holders_to_profile: int = Field(default=5, ge=0, le=20)
    bundle_max_pages: int = Field(default=3, ge=1, le=10)
    deep_analysis_min_prescore: Decimal = Decimal("60")
    security: SecurityThresholds = SecurityThresholds()
    weights: ScoringWeights = ScoringWeights()
    bounds: ScoringBounds = ScoringBounds()
    reject_below: Decimal = Decimal("60")
    watch_below: Decimal = Decimal("75")
    priority_from: Decimal = Decimal("85")

    def version_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()
