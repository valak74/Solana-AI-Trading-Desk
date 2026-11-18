"""Text and HTML renderings of the measurement report (French, self-contained)."""

from decimal import Decimal
from html import escape

from backend.reporting.report import Breakdown, Report, StrategyReport
from backend.reporting.stats import Interval, PerformanceSummary

LAMPORTS = Decimal(10**9)
STRATEGY_NAMES = {
    "A": "A — momentum confirmé",
    "B": "B — repli après graduation",
    "C": "C — témoin aléatoire",
}


def sol(lamports: int | Decimal | None, digits: int = 4) -> str:
    if lamports is None:
        return "—"
    return f"{Decimal(lamports) / LAMPORTS:+.{digits}f} SOL"


def pct(value: Decimal | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f} %"


def pf(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return "∞ (aucune perte)" if value.is_infinite() else str(value)


def interval_sol(interval: Interval | None) -> str:
    if interval is None:
        return "—"
    return f"[{sol(interval.low)} ; {sol(interval.high)}]"


def eur(lamports: int, report: Report) -> str:
    return f"{Decimal(lamports) / LAMPORTS * report.config.sol_eur:+.2f} €"


def _summary_lines(s: PerformanceSummary) -> list[str]:
    return [
        f"tentatives {s.attempts} (dont {s.failed_entries} achats échoués), "
        f"taux de réussite {pct(s.win_rate)}",
        f"PnL total {sol(s.total_pnl)}, gain moyen {sol(s.expectancy)} "
        f"IC95 {interval_sol(s.expectancy_ci)}",
        f"profit factor {pf(s.profit_factor)}, drawdown max {pct(s.max_drawdown)}",
    ]


def render_text(report: Report) -> str:
    lines = [
        f"RAPPORT DE MESURE — {report.run_id} — PAPER TRADING (simulation, aucun gain réel)",
        f"Période {report.start:%Y-%m-%d %H:%M} → {report.end:%Y-%m-%d %H:%M} UTC "
        f"({report.days:.1f} j), données manquantes {pct(report.gap_share)}",
        f"Période réservée (validation) depuis {report.holdout_start:%Y-%m-%d %H:%M}",
        f"Coût d'infrastructure sur la période : {report.infra_cost_eur} € "
        f"(hypothèse {report.config.monthly_infra_eur} €/mois, {report.config.sol_eur} €/SOL)",
        "",
        f"VERDICT GLOBAL : {report.verdict}",
    ]
    for s in report.strategies:
        lines += ["", f"[{STRATEGY_NAMES.get(s.name, s.name)}] verdict : {s.verdict}"]
        lines += ["  " + line for line in _summary_lines(s.overall)]
        lines.append(f"  écart avec le témoin C (gain moyen) : {interval_sol(s.vs_control)}")
        lines.append(
            f"  gain mensuel projeté : "
            f"{'—' if s.monthly_pnl_eur is None else f'{s.monthly_pnl_eur:+.2f} €'}"
        )
        lines.append(
            f"  exclues : {s.excluded_gap} pendant une coupure, "
            f"{s.excluded_interrupted} interrompues par un redémarrage"
        )
        lines += [f"  ✗ {reason}" for reason in s.reasons]
    if report.bands:
        lines += ["", f"Issues à {report.config.outcome_horizon} min par classement M3 :"]
        for b in report.bands:
            lines.append(
                f"  {b.band:<10} {b.tokens:>6} tokens,"
                f" hausse max médiane {pct(b.median_max_return)},"
                f" baisse max médiane {pct(b.median_min_return)}, atteignent +25 % "
                f"{pct(b.share_reaching_tp1)}, gradués {pct(b.share_graduated)}"
            )
    lines += ["", "Ce verdict n'autorise jamais seul le passage en réel : décision de l'opérateur."]
    return "\n".join(lines)


def render_short(report: Report) -> str:
    """Compact weekly summary (Telegram)."""
    parts = [
        f"Mesure {report.days:.1f} j — verdict {report.verdict}",
        f"données manquantes {pct(report.gap_share)}",
    ]
    for s in report.strategies:
        o = s.overall
        parts.append(
            f"{s.name}: {o.attempts} tent., PnL {sol(o.total_pnl, 3)} ({eur(o.total_pnl, report)}),"
            f" moy. {sol(o.expectancy, 4)} IC {interval_sol(o.expectancy_ci)},"
            f" PF {pf(o.profit_factor)},"
            f" DD {pct(o.max_drawdown)} → {s.verdict}"
        )
    parts.append("Simulation uniquement (paper trading).")
    return "\n".join(parts)


# --- HTML --------------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e3e3e0;--good:#1a7f4b;
--bad:#b42318;--warn:#a15c00;--accent:#3b5bdb}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececee;--muted:#9a9aa2;
--card:#1c1c20;--line:#2c2c32;--good:#4cc38a;--bad:#ff6b5e;--warn:#f0a845;--accent:#8ea2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}
.muted{color:var(--muted)}.banner{border:1px solid var(--warn);color:var(--warn);
border-radius:8px;padding:8px 12px;margin:12px 0}
.verdict{display:inline-block;font-weight:700;padding:4px 10px;border-radius:6px;
border:1px solid currentColor}.GO{color:var(--good)}.STOP{color:var(--bad)}
.PROLONGER,.PROMETTEUR,.DONNÉES{color:var(--warn)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{margin:0 0 8px}.card h3{font-size:14px;margin:16px 0 4px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500}td.n{text-align:right}
.scroll{overflow-x:auto}ul.reasons{margin:6px 0 0;padding-left:18px;color:var(--bad)}
svg{width:100%;height:120px;display:block}
"""


def _row(label: str, value: str) -> str:
    return f"<tr><th>{escape(label)}</th><td class='n'>{escape(value)}</td></tr>"


def _curve_svg(s: StrategyReport) -> str:
    if len(s.curve) < 2:
        return "<p class='muted'>Pas assez de positions pour tracer la courbe.</p>"
    values = [v for _, v in s.curve]
    low, high = min(0, *values), max(0, *values)
    span = (high - low) or 1
    n = len(values) - 1
    points = " ".join(
        f"{i / n * 1000:.1f},{110 - (v - low) / span * 100:.1f}" for i, v in enumerate(values)
    )
    zero = 110 - (0 - low) / span * 100
    color = "var(--good)" if values[-1] >= 0 else "var(--bad)"
    return (
        "<svg viewBox='0 0 1000 120' preserveAspectRatio='none' role='img' "
        f"aria-label='PnL cumulé {escape(s.name)}'>"
        f"<line x1='0' x2='1000' y1='{zero:.1f}' y2='{zero:.1f}' stroke='var(--line)'/>"
        f"<polyline fill='none' stroke='{color}' stroke-width='2' "
        f"vector-effect='non-scaling-stroke' points='{points}'/></svg>"
    )


def _breakdown_table(title: str, rows: dict[str, Breakdown] | dict[int, Breakdown]) -> str:
    if not rows:
        return ""
    body = "".join(
        f"<tr><td>{escape(str(k))}</td><td class='n'>{b.count}</td>"
        f"<td class='n'>{escape(sol(b.total_pnl))}</td>"
        f"<td class='n'>{escape(sol(b.mean_pnl))}</td></tr>"
        for k, b in rows.items()
    )
    return (
        f"<h3>{escape(title)}</h3><div class='scroll'><table><tr><th></th><th>n</th>"
        f"<th>PnL total</th><th>moyen</th></tr>{body}</table></div>"
    )


def _strategy_card(s: StrategyReport, report: Report) -> str:
    o = s.overall
    reasons = "".join(f"<li>{escape(r)}</li>" for r in s.reasons)
    table = "".join(
        [
            _row("Tentatives (achats échoués)", f"{o.attempts} ({o.failed_entries})"),
            _row(
                "PnL total",
                "—" if not o.attempts else f"{sol(o.total_pnl)} ({eur(o.total_pnl, report)})",
            ),
            _row("Gain moyen par tentative", sol(o.expectancy)),
            _row("Intervalle de confiance 95 %", interval_sol(o.expectancy_ci)),
            _row("Rendement moyen", pct(o.return_per_attempt, 2)),
            _row("Taux de réussite", pct(o.win_rate)),
            _row("Gain / perte moyens", f"{sol(o.avg_win)} / {sol(o.avg_loss)}"),
            _row("Profit factor", pf(o.profit_factor)),
            _row("Drawdown max", pct(o.max_drawdown)),
            _row("Écart avec le témoin C", interval_sol(s.vs_control)),
            _row(
                "Apprentissage / réservée (gain moyen)",
                f"{sol(s.learning.expectancy)} / {sol(s.holdout.expectancy)}",
            ),
            _row("Frais réseau moyens", sol(s.avg_network_fee, 6)),
            _row("Frais protocole moyens", sol(s.avg_protocol_fee, 6)),
            _row("Écart d'exécution moyen", pct(s.avg_slippage, 2)),
            _row("Taux d'échec des transactions", pct(s.fill_failure_rate)),
            _row(
                "Gain mensuel projeté",
                "—" if s.monthly_pnl_eur is None else f"{s.monthly_pnl_eur:+.2f} €",
            ),
            _row("Exclues (coupure / redémarrage)", f"{s.excluded_gap} / {s.excluded_interrupted}"),
        ]
    )
    css_class = s.verdict.split(" ")[0].rstrip(",")
    return (
        f"<section class='card'><h2>{escape(STRATEGY_NAMES.get(s.name, s.name))}</h2>"
        f"<span class='verdict {escape(css_class)}'>{escape(s.verdict)}</span>"
        f"{f'<ul class=reasons>{reasons}</ul>' if reasons else ''}"
        f"{_curve_svg(s)}<table>{table}</table>"
        f"{_breakdown_table('Par raison de sortie', s.by_exit)}"
        f"{_breakdown_table('Par score à l entrée', s.by_score)}"
        f"{_breakdown_table('Par heure d ouverture (UTC)', s.by_hour)}</section>"
    )


def render_html(report: Report) -> str:
    bands = "".join(
        f"<tr><td>{escape(b.band)}</td><td class='n'>{b.tokens}</td>"
        + "".join(
            f"<td class='n'>{pct(v)}</td>"
            for v in (
                b.median_max_return,
                b.median_min_return,
                b.share_reaching_tp1,
                b.share_graduated,
            )
        )
        + "</tr>"
        for b in report.bands
    )
    bands_html = (
        f"<h2>Issues à {report.config.outcome_horizon} min selon le classement M3</h2>"
        "<p class='muted'>Tous les tokens analysés, tradés ou non. Si le score est utile, les "
        "classements élevés doivent montrer de meilleures issues.</p><div class='scroll'><table>"
        "<tr><th>Classement</th><th>Tokens</th><th>Hausse max médiane</th>"
        "<th>Baisse max médiane</th><th>Atteignent +25 %</th><th>Gradués</th></tr>"
        f"{bands}</table></div>"
        if bands
        else ""
    )
    cards = "".join(_strategy_card(s, report) for s in report.strategies)
    css_class = report.verdict.split(" ")[0]
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rapport de mesure</title><style>{CSS}</style></head><body><main>
<h1>Rapport de mesure — {escape(report.run_id)}</h1>
<p class="muted">{report.start:%d/%m/%Y %H:%M} → {report.end:%d/%m/%Y %H:%M} UTC
({report.days:.1f} jours) · données manquantes {pct(report.gap_share)} ·
période réservée depuis le {report.holdout_start:%d/%m/%Y %H:%M} · généré le
{report.generated_at:%d/%m/%Y %H:%M} UTC</p>
<div class="banner">Paper trading : simulation uniquement. Aucun de ces résultats n'est un gain
réel. Ce verdict n'autorise jamais seul le passage en réel.</div>
<p>Verdict global : <span class="verdict {escape(css_class)}">{escape(report.verdict)}</span>
· coût d'infrastructure sur la période : {report.infra_cost_eur} €
(hypothèses : {report.config.monthly_infra_eur} €/mois, {report.config.sol_eur} €/SOL)</p>
<div class="cards">{cards}</div>{bands_html}
</main></body></html>"""
