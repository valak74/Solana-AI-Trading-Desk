import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.reporting.dataset import OutcomeLabel, PositionResult
from backend.reporting.render import render_html, render_short, render_text
from backend.reporting.report import ReportConfig, build_report, gap_share, score_bucket
from backend.reporting.stats import (
    bootstrap_mean,
    bootstrap_mean_difference,
    max_drawdown,
    profit_factor,
    summarize,
)

T0 = datetime(2026, 11, 1, tzinfo=UTC)
END = T0 + timedelta(days=30)
CFG = ReportConfig(resamples=500)


# --- statistics --------------------------------------------------------------------------


def test_bootstrap_is_reproducible_and_brackets_the_mean() -> None:
    rng = random.Random(3)
    values = [rng.randint(-100, 140) for _ in range(300)]
    first, second = bootstrap_mean(values, seed=1), bootstrap_mean(values, seed=1)
    assert first == second and first is not None
    true_mean = Decimal(sum(values)) / len(values)
    assert first.low < true_mean < first.high
    assert bootstrap_mean([1]) is None


def test_difference_interval_detects_a_clear_gap() -> None:
    better = [100 + (i % 7) for i in range(80)]
    worse = [-50 + (i % 5) for i in range(80)]
    interval = bootstrap_mean_difference(better, worse, resamples=500)
    assert interval is not None and interval.low > 100
    same = bootstrap_mean_difference(worse, worse, resamples=500)
    assert same is not None and same.low < 0 < same.high


def test_profit_factor_and_drawdown() -> None:
    assert profit_factor([30, -10, -10]) == Decimal("1.5")
    assert profit_factor([5]) == Decimal("Infinity")
    assert profit_factor([]) is None
    # Equity 100 -> 150 -> 75 -> 90: worst fall 75/150 = 50 %.
    assert max_drawdown([50, -75, 15], 100) == Decimal("0.5")
    assert max_drawdown([10, 10], 100) == Decimal(0)


def test_summary_counts_failed_entries_as_costs_but_not_as_trades() -> None:
    s = summarize([100, -55, -40], [1000, 55, 1000], [True, False, True], 10_000, resamples=200)
    assert (s.attempts, s.filled, s.failed_entries) == (3, 2, 1)
    assert s.total_pnl == 5
    assert s.win_rate == Decimal("0.5")
    assert s.expectancy == Decimal("1.6667")


# --- report and verdicts -----------------------------------------------------------------


def result(
    strategy: str,
    i: int,
    pnl: int | None,
    *,
    reason: str = "take_profit_1",
    score: Decimal | None = Decimal(80),
    spacing: timedelta = timedelta(minutes=10),
) -> PositionResult:
    opened = T0 + spacing * i
    return PositionResult(
        strategy,
        f"m{i}",
        opened,
        opened + timedelta(minutes=5),
        pnl,
        60_000_000,
        reason,
        110_000,
        1_000_000,
        Decimal("0.012"),
        2,
        0,
        score,
    )


def scenario(a_pnl: int, c_pnl: int, n: int = 150) -> list[PositionResult]:
    """Positions spread over the whole period, so the reserved last 30 % holds some too."""
    spacing = (END - T0) / (n + 1)
    rows = []
    for i in range(n):
        noise = (i % 5 - 2) * 2_000_000
        rows.append(result("A", i, a_pnl + noise, spacing=spacing))
        rows.append(result("C", i, c_pnl + noise, score=Decimal(65), spacing=spacing))
    return rows


def report_for(rows: list[PositionResult], end: datetime = END, **cfg: object):  # type: ignore[no-untyped-def]
    config = CFG.model_copy(update=cfg)
    return build_report(
        rows, [], [], run_id="live", start=T0, end=end, generated_at=end, config=config
    )


def test_empty_reserved_period_blocks_go() -> None:
    """All results in the first day: nothing confirms them on the reserved period."""
    rows = [result("A", i, 6_000_000 + (i % 5 - 2) * 2_000_000) for i in range(150)]
    rows += [result("C", i, -3_000_000) for i in range(150)]
    a = next(s for s in report_for(rows).strategies if s.name == "A")
    assert a.verdict != "GO" and "non confirmée sur la période réservée" in a.reasons


def test_go_requires_every_criterion() -> None:
    report = report_for(scenario(a_pnl=6_000_000, c_pnl=-3_000_000))
    a = next(s for s in report.strategies if s.name == "A")
    assert a.verdict == "GO", a.reasons
    assert report.verdict == "GO"
    assert next(s for s in report.strategies if s.name == "C").verdict == "TÉMOIN"


def test_not_better_than_control_is_not_go() -> None:
    report = report_for(scenario(a_pnl=3_000_000, c_pnl=3_000_000))
    a = next(s for s in report.strategies if s.name == "A")
    assert a.verdict == "PROMETTEUR, NON CONCLUANT"
    assert any("témoin" in r for r in a.reasons)
    assert report.verdict == "PROLONGER"


def test_losing_strategy_is_stop() -> None:
    report = report_for(scenario(a_pnl=-4_000_000, c_pnl=-3_000_000))
    a = next(s for s in report.strategies if s.name == "A")
    assert a.verdict == "STOP"


def test_infrastructure_cost_can_block_go() -> None:
    report = report_for(scenario(a_pnl=6_000_000, c_pnl=-3_000_000), monthly_infra_eur=Decimal(500))
    a = next(s for s in report.strategies if s.name == "A")
    assert a.verdict != "GO"
    assert any("infrastructure" in r for r in a.reasons)


def test_insufficient_data() -> None:
    report = report_for(scenario(6_000_000, -3_000_000, n=20), end=T0 + timedelta(days=5))
    a = next(s for s in report.strategies if s.name == "A")
    assert a.verdict == "DONNÉES INSUFFISANTES" and report.verdict == "PROLONGER"


def test_exclusions_holdout_and_breakdowns() -> None:
    rows = [
        result("A", 0, 10_000_000),
        result("A", 1, None, reason="interrupted_by_restart"),
        result("A", 2, -55_000, reason="entry_failed:simulated_failure"),
        result("A", 3, 5_000_000),  # overlaps the gap below
        result("A", 3100, 7_000_000),  # day ~21.5: inside the reserved last 30 %
    ]
    gap = (T0 + timedelta(minutes=31), T0 + timedelta(minutes=40))
    report = build_report(
        rows, [gap], [], run_id="live", start=T0, end=END, generated_at=END, config=CFG
    )
    a = next(s for s in report.strategies if s.name == "A")
    assert (a.excluded_interrupted, a.excluded_gap) == (1, 1)
    assert (a.overall.attempts, a.overall.failed_entries) == (3, 1)
    assert (a.learning.attempts, a.holdout.attempts) == (2, 1)
    assert set(a.by_exit) == {"take_profit_1", "entry_failed:simulated_failure"}
    assert a.curve[-1][1] == 10_000_000 - 55_000 + 7_000_000


def test_score_buckets_and_gap_share() -> None:
    assert [
        score_bucket(v) for v in (None, Decimal(59), Decimal(60), Decimal(80), Decimal(90))
    ] == ["inconnu", "< 60", "60-74", "75-84", "85+"]
    assert gap_share([(T0, T0 + timedelta(days=3))], T0, END) == Decimal("0.1")


def test_outcome_bands() -> None:
    labels = [
        OutcomeLabel("candidate", 30, Decimal("0.4"), Decimal("-0.1"), None, True, False),
        OutcomeLabel("candidate", 30, Decimal("0.1"), Decimal("-0.3"), None, False, False),
        OutcomeLabel("reject", 30, Decimal("0.0"), Decimal("-0.5"), None, False, False),
        OutcomeLabel("reject", 30, Decimal("2.0"), Decimal("0"), None, False, True),  # gap
        OutcomeLabel("reject", 15, Decimal("2.0"), Decimal("0"), None, False, False),
    ]
    report = build_report(
        [], [], labels, run_id="live", start=T0, end=END, generated_at=END, config=CFG
    )
    bands = {b.band: b for b in report.bands}
    assert [b.band for b in report.bands] == ["reject", "candidate"]
    assert bands["candidate"].share_reaching_tp1 == Decimal("0.5")
    assert bands["candidate"].share_graduated == Decimal("0.5")
    assert bands["reject"].tokens == 1


def test_renderings_are_honest_and_escaped() -> None:
    rows = scenario(6_000_000, -3_000_000, n=120)
    rows.append(result("A", 999, 1, reason="<script>alert(1)</script>"))
    report = report_for(rows)
    text, html, short = render_text(report), render_html(report), render_short(report)
    assert "PAPER TRADING" in text and "aucun gain réel" in text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "Simulation" in short and "verdict" in short
    assert "<svg" in html and 'lang="fr"' in html
