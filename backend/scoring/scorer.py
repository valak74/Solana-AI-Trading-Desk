"""Deterministic opportunity score (0-100) from measurable features.

Components follow the specification's weights. While sentiment is not implemented, it is
*excluded* and the remaining weights are renormalised (it is not counted as zero). Any other
missing component counts as 0: missing data must never make a token look better.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from backend.security.config import AnalysisConfig

Q = Decimal("0.01")
COMPONENTS = ("liquidity", "momentum", "wallet", "holder", "volume", "sentiment")


def ramp(value: Decimal | int | None, zero: Decimal | int, full: Decimal | int) -> Decimal | None:
    """Linear 0..100 between ``zero`` and ``full`` (works for increasing or decreasing ramps)."""
    if value is None:
        return None
    v, z, f = Decimal(value), Decimal(zero), Decimal(full)
    if z == f:
        return Decimal(100) if v >= f else Decimal(0)
    fraction = (v - z) / (f - z)
    return (min(Decimal(1), max(Decimal(0), fraction)) * 100).quantize(Q, ROUND_HALF_EVEN)


def _weighted(parts: list[tuple[Decimal | None, Decimal]]) -> Decimal | None:
    present = [(v, w) for v, w in parts if v is not None]
    if not present:
        return None
    total_weight = sum((w for _, w in present), Decimal(0))
    return (sum((v * w for v, w in present), Decimal(0)) / total_weight).quantize(Q)


@dataclass(frozen=True, slots=True)
class ScoreInputs:
    entry_price_impact: Decimal | None = None
    buy_ratio: Decimal | None = None
    acceleration: Decimal | None = None
    top10_share: Decimal | None = None
    creator_share: Decimal | None = None
    unique_buyers: int | None = None
    wash_ratio: Decimal | None = None
    fresh_holder_ratio: Decimal | None = None  # None until deep analysis ran
    largest_funding_cluster: int | None = None
    creation_slot_txs: int | None = None
    sentiment: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    components: dict[str, Decimal | None]
    total: Decimal
    band: str
    missing: list[str] = field(default_factory=list)

    def as_features(self) -> dict[str, Any]:
        return {
            "components": {k: None if v is None else str(v) for k, v in self.components.items()},
            "band": self.band,
            "missing_components": self.missing,
        }


def _wallet_component(i: ScoreInputs) -> Decimal | None:
    if i.fresh_holder_ratio is None:
        return None
    score = Decimal(100) - i.fresh_holder_ratio * 60
    score -= Decimal(min(i.creation_slot_txs or 0, 10)) * 3
    score -= Decimal(max(0, (i.largest_funding_cluster or 0) - 1)) * 10
    return max(Decimal(0), score).quantize(Q)


def component_scores(i: ScoreInputs, cfg: AnalysisConfig) -> dict[str, Decimal | None]:
    b = cfg.bounds
    return {
        "liquidity": ramp(i.entry_price_impact, b.impact_zero, b.impact_full),
        "momentum": _weighted(
            [
                (ramp(i.buy_ratio, b.buy_ratio_zero, b.buy_ratio_full), Decimal("0.6")),
                (ramp(i.acceleration, b.acceleration_zero, b.acceleration_full), Decimal("0.4")),
            ]
        ),
        "wallet": _wallet_component(i),
        "holder": _weighted(
            [
                (ramp(i.top10_share, b.top10_zero, b.top10_full), Decimal("0.8")),
                (ramp(i.creator_share, b.creator_zero, b.creator_full), Decimal("0.2")),
            ]
        ),
        "volume": _weighted(
            [
                (ramp(i.unique_buyers, b.unique_buyers_zero, b.unique_buyers_full), Decimal("0.6")),
                (ramp(i.wash_ratio, b.wash_zero, b.wash_full), Decimal("0.4")),
            ]
        ),
        "sentiment": i.sentiment,
    }


def band_for(total: Decimal, cfg: AnalysisConfig) -> str:
    if total < cfg.reject_below:
        return "reject"
    if total < cfg.watch_below:
        return "watch"
    if total < cfg.priority_from:
        return "candidate"
    return "priority"


def score(i: ScoreInputs, cfg: AnalysisConfig, *, exclude: tuple[str, ...] = ()) -> ScoreBreakdown:
    """Total score. ``exclude`` drops components from the computation (pre-score)."""
    components = component_scores(i, cfg)
    weights = cfg.weights.model_dump()
    missing = [k for k, v in components.items() if v is None and k != "sentiment"]
    skipped = {*exclude, *(("sentiment",) if i.sentiment is None else ())}
    used = [k for k in COMPONENTS if k not in skipped]
    total_weight = sum(Decimal(weights[k]) for k in used)
    weighted = sum(((components[k] or Decimal(0)) * Decimal(weights[k]) for k in used), Decimal(0))
    total = (weighted / total_weight).quantize(Q) if total_weight else Decimal(0)
    return ScoreBreakdown(components, total, band_for(total, cfg), missing)
