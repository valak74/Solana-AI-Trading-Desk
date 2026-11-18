"""Measurement report from the database alone.

python -m backend.reporting --sol-eur 150 --infra-eur 15 --html rapport.html
python -m backend.reporting --run-id replay-20261201-1200
"""

import argparse
import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from backend.config import get_settings
from backend.database.session import create_engine, create_session_factory
from backend.reporting.render import render_html, render_text
from backend.reporting.report import ReportConfig
from backend.reporting.service import generate_report


async def _run(args: argparse.Namespace) -> int:
    engine = create_engine(get_settings().database_url.get_secret_value())
    try:
        config = ReportConfig(
            sol_eur=Decimal(args.sol_eur), monthly_infra_eur=Decimal(args.infra_eur)
        )
        report = await generate_report(
            create_session_factory(engine),
            config,
            run_id=args.run_id,
            start=datetime.fromisoformat(args.start) if args.start else None,
            end=datetime.fromisoformat(args.end) if args.end else None,
        )
    finally:
        await engine.dispose()
    if report is None:
        print("Aucune donnée enregistrée pour cette période.")
        return 1
    print(render_text(report))
    if args.html:
        await asyncio.to_thread(Path(args.html).write_text, render_html(report), "utf-8")
        print(f"\nRapport HTML écrit : {args.html}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Rapport de mesure (paper trading)")
    parser.add_argument("--run-id", default="live")
    parser.add_argument("--sol-eur", default="150", help="cours SOL/EUR pour les conversions")
    parser.add_argument("--infra-eur", default="15", help="coût d'infrastructure mensuel (EUR)")
    parser.add_argument("--start", default=None, help="début ISO 8601 (défaut : 1er événement)")
    parser.add_argument("--end", default=None, help="fin ISO 8601 (défaut : maintenant)")
    parser.add_argument("--html", default=None, help="fichier HTML à écrire")
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
