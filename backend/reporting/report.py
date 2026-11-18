"""Measurement report and GO / EXTEND / STOP verdict (docs/DEVELOPMENT_PLAN.md, section 4.3).

The verdict is mechanical and conservative. It never authorises live trading by itself: going
live remains an explicit operator decision (and requires steps G1-G5 first).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from pydantic import BaseModel, ConfigDict

from backend.reporting.dataset import OutcomeLabel, PositionResult, overlaps
from backend.reporting.stats import (
    Interval,
    PerformanceSummary,
    bootstrap_mean_difference,
    summarize,
)

LAMPORTS = Decimal(10**9)
CONTROL = "C"


class ReportConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    sol_eur: Decimal = Decimal(150)
    monthly_infra_eur: Decimal = Decimal(15)
    starting_equity_lamports: int = 10**9
    min_attempts: int = 100
    min_days: float = 21
    holdout_fraction: Decimal = Decimal("0.30")
    min_profit_factor: Decimal = Decimal("1.3")
    max_drawdown: Decimal = Decimal("0.10")
    confidence: Decimal = Decimal("0.95")
    resamples: int = 2000
    outcome_horizon: int = 30


@dataclass(frozen=True, slots=True)
class Breakdown:
    count: int
    total_pnl: int
    mean_pnl: Decimal | None


@dataclass(slots=True)
class StrategyReport:
    name: str
    overall: PerformanceSummary
    learning: PerformanceSummary
    holdout: PerformanceSummary
    excluded_gap: int
    excluded_interrupted: int
    avg_network_fee: Decimal | None
    avg_protocol_fee: Decimal | None
    avg_slippage: Decimal | None
    fill_failure_rate: Decimal | None
    by_exit: dict[str, Breakdown] = field(default_factory=dict)
    by_hour: dict[int, Breakdown] = field(default_factory=dict)
    by_score: dict[str, Breakdown] = field(default_factory=dict)
    vs_control: Interval | None = None
    monthly_pnl_eur: Decimal | None = None
    verdict: str = "-"
    reasons: list[str] = field(default_factory=list)
    curve: list[tuple[datetime, int]] = field(default_factory=list)  # cumulative PnL


@dataclass(frozen=True, slots=True)
class BandOutcome:
    band: str
    tokens: int
    median_max_return: Decimal | None
    median_min_return: Decimal | None
    share_reaching_tp1: Decimal | None
    share_graduated: Decimal | None


@dataclass(slots=True)
class Report:
    generated_at: datetime
    run_id: str
    start: datetime
    end: datetime
    holdout_start: datetime
    gap_share: Decimal
    config: ReportConfig
    strategies: list[StrategyReport]
    bands: list[BandOutcome]
    verdict: str
    infra_cost_eur: Decimal

    @property
    def days(self) -> Decimal:
        return Decimal((self.end - self.start).total_seconds()) / Decimal(86_400)


def _breakdown(items: list[PositionResult]) -> Breakdown:
    pnls = [p.pnl or 0 for p in items]
    return Breakdown(len(pnls), sum(pnls), Decimal(sum(pnls)) / len(pnls) if pnls else None)


def score_bucket(score: Decimal | None) -> str:
    if score is None:
        return "inconnu"
    if score < 60:
        return "< 60"
    if score < 75:
        return "60-74"
    if score < 85:
        return "75-84"
    return "85+"


def _summary(items: list[PositionResult], cfg: ReportConfig) -> PerformanceSummary:
    return summarize(
        [p.pnl or 0 for p in items],
        [p.cost for p in items],
        [p.filled for p in items],
        cfg.starting_equity_lamports,
        resamples=cfg.resamples,
        confidence=cfg.confidence,
    )


def _avg(values: list[int] | list[Decimal]) -> Decimal | None:
    return Decimal(sum(values, Decimal(0))) / len(values) if values else None


def _strategy_report(
    name: str,
    positions: list[PositionResult],
    gaps: list[tuple[datetime, datetime]],
    holdout_start: datetime,
    days: Decimal,
    cfg: ReportConfig,
) -> StrategyReport:
    interrupted = [p for p in positions if p.interrupted or p.pnl is None]
    usable = [p for p in positions if p not in interrupted]
    in_gap = [p for p in usable if overlaps(p.opened_at, p.closed_at, gaps)]
    valid = sorted((p for p in usable if p not in in_gap), key=lambda p: p.closed_at)
    filled = [p for p in valid if p.filled]
    attempts = sum(p.fill_attempts for p in valid)
    slippages = [p.slippage for p in filled if p.slippage is not None]
    groups: dict[str, defaultdict[object, list[PositionResult]]] = {
        "exit": defaultdict(list),
        "hour": defaultdict(list),
        "score": defaultdict(list),
    }
    for p in valid:
        groups["exit"][p.exit_reason].append(p)
        groups["hour"][p.opened_at.hour].append(p)
        groups["score"][score_bucket(p.score_at_entry)].append(p)
    cumulative, curve = 0, []
    for p in valid:
        cumulative += p.pnl or 0
        curve.append((p.closed_at, cumulative))
    overall = _summary(valid, cfg)
    monthly = None
    if days > 0:
        monthly = (Decimal(overall.total_pnl) / LAMPORTS * cfg.sol_eur * 30 / days).quantize(
            Decimal("0.01")
        )
    return StrategyReport(
        name=name,
        overall=overall,
        learning=_summary([p for p in valid if p.opened_at < holdout_start], cfg),
        holdout=_summary([p for p in valid if p.opened_at >= holdout_start], cfg),
        excluded_gap=len(in_gap),
        excluded_interrupted=len(interrupted),
        avg_network_fee=_avg([p.network_fees for p in valid]),
        avg_protocol_fee=_avg([p.protocol_fees for p in filled]),
        avg_slippage=_avg(slippages),
        fill_failure_rate=(
            Decimal(sum(p.fill_failures for p in valid)) / attempts if attempts else None
        ),
        by_exit={str(k): _breakdown(v) for k, v in sorted(groups["exit"].items(), key=str)},
        by_hour={int(k): _breakdown(v) for k, v in sorted(groups["hour"].items())},  # type: ignore[call-overload]
        by_score={str(k): _breakdown(v) for k, v in sorted(groups["score"].items(), key=str)},
        monthly_pnl_eur=monthly,
        curve=curve,
    )


def _judge(s: StrategyReport, days: Decimal, cfg: ReportConfig) -> None:
    o, reasons = s.overall, s.reasons
    if o.attempts < cfg.min_attempts or days < Decimal(str(cfg.min_days)):
        s.verdict = "DONNÉES INSUFFISANTES"
        reasons.append(
            f"{o.attempts} tentatives sur {days:.1f} j "
            f"(minimum {cfg.min_attempts} et {cfg.min_days:g} j)"
        )
        return
    checks = [
        (
            o.expectancy_ci is not None and o.expectancy_ci.low > 0,
            "gain moyen pas significativement positif (borne basse de l'intervalle ≤ 0)",
        ),
        (
            o.profit_factor is not None and o.profit_factor >= cfg.min_profit_factor,
            f"profit factor < {cfg.min_profit_factor}",
        ),
        (o.max_drawdown < cfg.max_drawdown, f"drawdown ≥ {cfg.max_drawdown:.0%}"),
        (
            s.vs_control is not None and s.vs_control.low > 0,
            "pas nettement meilleure que le témoin aléatoire C",
        ),
        (
            s.holdout.expectancy is not None and s.holdout.expectancy > 0,
            "non confirmée sur la période réservée",
        ),
        (
            s.monthly_pnl_eur is not None and s.monthly_pnl_eur > cfg.monthly_infra_eur,
            "gain mensuel projeté inférieur au coût d'infrastructure",
        ),
    ]
    reasons.extend(message for ok, message in checks if not ok)
    if not reasons:
        s.verdict = "GO"
    elif o.expectancy is not None and o.expectancy > 0:
        s.verdict = "PROMETTEUR, NON CONCLUANT"
    else:
        s.verdict = "STOP"


def _bands(labels: list[OutcomeLabel], horizon: int) -> list[BandOutcome]:
    grouped: defaultdict[str, list[OutcomeLabel]] = defaultdict(list)
    for label in labels:
        if label.horizon == horizon and not label.gap_overlap:
            grouped[label.band].append(label)
    order = {"reject": 0, "watch": 1, "candidate": 2, "priority": 3}
    result = []
    for band in sorted(grouped, key=lambda b: order.get(b, 9)):
        items = grouped[band]
        maxes = [i.max_return for i in items if i.max_return is not None]
        mins = [i.min_return for i in items if i.min_return is not None]
        result.append(
            BandOutcome(
                band=band,
                tokens=len(items),
                median_max_return=Decimal(median(maxes)) if maxes else None,
                median_min_return=Decimal(median(mins)) if mins else None,
                share_reaching_tp1=(
                    Decimal(sum(1 for m in maxes if m >= Decimal("0.25"))) / len(maxes)
                    if maxes
                    else None
                ),
                share_graduated=Decimal(sum(1 for i in items if i.graduated)) / len(items),
            )
        )
    return result


def gap_share(gaps: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> Decimal:
    total = (end - start).total_seconds()
    if total <= 0:
        return Decimal(0)
    covered = sum(max(0.0, (min(e, end) - max(s, start)).total_seconds()) for s, e in gaps)
    return Decimal(str(round(min(covered, total) / total, 4)))


def build_report(
    positions: list[PositionResult],
    gaps: list[tuple[datetime, datetime]],
    labels: list[OutcomeLabel],
    *,
    run_id: str,
    start: datetime,
    end: datetime,
    generated_at: datetime,
    config: ReportConfig,
) -> Report:
    span = end - start
    holdout_start = start + timedelta(
        seconds=span.total_seconds() * float(1 - config.holdout_fraction)
    )
    days = Decimal(span.total_seconds()) / Decimal(86_400)
    names = sorted({p.strategy for p in positions} | {"A", "B", "C"})
    reports = [
        _strategy_report(
            n, [p for p in positions if p.strategy == n], gaps, holdout_start, days, config
        )
        for n in names
    ]
    by_name = {r.name: r for r in reports}
    control = by_name.get(CONTROL)
    control_pnls = [] if control is None else _valid_pnls(positions, CONTROL, gaps)
    for report in reports:
        if report.name == CONTROL:
            report.verdict = "TÉMOIN"
            continue
        report.vs_control = bootstrap_mean_difference(
            _valid_pnls(positions, report.name, gaps),
            control_pnls,
            resamples=config.resamples,
            confidence=config.confidence,
        )
        _judge(report, days, config)
    verdicts = {r.verdict for r in reports if r.name != CONTROL}
    if "GO" in verdicts:
        overall = "GO"
    elif verdicts & {"PROMETTEUR, NON CONCLUANT", "DONNÉES INSUFFISANTES"}:
        overall = "PROLONGER"
    else:
        overall = "STOP"
    return Report(
        generated_at=generated_at,
        run_id=run_id,
        start=start,
        end=end,
        holdout_start=holdout_start,
        gap_share=gap_share(gaps, start, end),
        config=config,
        strategies=reports,
        bands=_bands(labels, config.outcome_horizon),
        verdict=overall,
        infra_cost_eur=(config.monthly_infra_eur * days / 30).quantize(Decimal("0.01")),
    )


def _valid_pnls(
    positions: list[PositionResult], strategy: str, gaps: list[tuple[datetime, datetime]]
) -> list[int]:
    return [
        p.pnl
        for p in positions
        if p.strategy == strategy
        and p.pnl is not None
        and not p.interrupted
        and not overlaps(p.opened_at, p.closed_at, gaps)
    ]
