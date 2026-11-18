"""Replay of the recorded market stream and analyses through a fresh paper engine.

    python -m backend.trading.replay --config variante.json
    python -m backend.trading.replay --include-holdout   # final validation only

What can be varied: everything in PaperTradingConfig (entries, exits, risk limits, execution
model). What cannot: the M3 analyses themselves (they depend on on-chain state at the time) and
Jupiter quotes (historical quotes do not exist), so strategy B cannot trade in a replay.

Discipline: by default the replay stops where the reserved validation period starts (last 30 %
of the measurement). Tuning parameters on that period would make the final check meaningless.
"""

import argparse
import asyncio
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.models import RawEvent, SecurityAnalysis, Token, TokenScore
from backend.execution.paper import PaperExecutor
from backend.reporting.dataset import load_gaps, measurement_start
from backend.risk.manager import RiskManager
from backend.scanner.providers.base import RawMessage
from backend.trading.config import PaperTradingConfig
from backend.trading.engine import PaperTradingEngine
from backend.trading.repository import SqlTradingStore
from backend.trading.strategies import AnalysisSignal

Factory = async_sessionmaker[AsyncSession]
MARKET_EVENTS = ("token_created", "trade", "migration")
TICK = timedelta(seconds=5)
BAND_RANK = {"reject": 0, "watch": 1, "candidate": 2, "priority": 3}


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_id: str
    start: datetime
    end: datetime
    events: int
    analyses: int
    holdout_start: datetime
    included_holdout: bool
    counters: dict[str, int]


def decision_from(critical: list[Any], features: dict[str, Any]) -> str:
    """Same rule as AnalysisResult.decision: critical -> reject; no deep analysis -> <= watch."""
    if critical:
        return "reject"
    band = str(features.get("band", "reject"))
    if not features.get("deep_analysis") and BAND_RANK.get(band, 0) > BAND_RANK["watch"]:
        return "watch"
    return band


async def load_signals(factory: Factory, start: datetime, end: datetime) -> list[AnalysisSignal]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(SecurityAnalysis, TokenScore, Token.mint_address)
                .join(
                    TokenScore,
                    (TokenScore.token_id == SecurityAnalysis.token_id)
                    & (TokenScore.created_at == SecurityAnalysis.created_at),
                )
                .join(Token, Token.id == SecurityAnalysis.token_id)
                .where(SecurityAnalysis.created_at >= start, SecurityAnalysis.created_at < end)
                .order_by(SecurityAnalysis.created_at, SecurityAnalysis.id)
            )
        ).all()
    return [
        AnalysisSignal(
            token_id=sa.token_id,
            mint=mint,
            at=sa.created_at,
            decision=decision_from(list(sa.critical_flags), dict(ts.features)),
            critical_flags=tuple(str(c) for c in sa.critical_flags),
            total_score=ts.total_score,
            metrics=dict(sa.metrics),
        )
        for sa, ts, mint in rows
    ]


async def stream_events(
    factory: Factory, start: datetime, end: datetime
) -> AsyncIterator[RawMessage]:
    async with factory() as session:
        result = await session.stream(
            select(RawEvent.source, RawEvent.received_at, RawEvent.payload)
            .where(
                RawEvent.event_type.in_(MARKET_EVENTS),
                RawEvent.received_at >= start,
                RawEvent.received_at < end,
            )
            .order_by(RawEvent.received_at, RawEvent.id)
            .execution_options(yield_per=5000)
        )
        async for source, received_at, payload in result:
            yield RawMessage(source, received_at, payload, 0)


async def replay(
    factory: Factory,
    config: PaperTradingConfig,
    *,
    run_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    include_holdout: bool = False,
    holdout_fraction: Decimal = Decimal("0.30"),
) -> ReplayResult:
    first = start or await measurement_start(factory)
    last = end or datetime.now(UTC)
    if first is None:
        raise ValueError("no recorded data to replay")
    holdout_start = first + (last - first) * float(1 - holdout_fraction)
    stop = last if include_holdout else min(last, holdout_start)

    gaps = await load_gaps(factory, first, stop)

    def feed_healthy(at: datetime) -> bool:
        return not any(s <= at < e for s, e in gaps)

    risk = RiskManager(config.risk, lambda: False, feed_healthy)
    executor = PaperExecutor(config.execution, random.Random(f"{config.seed}:execution"))  # noqa: S311
    engine = PaperTradingEngine(config, executor, risk, SqlTradingStore(factory), run_id=run_id)
    await engine.start(first)

    signals = await load_signals(factory, first, stop)
    next_signal, next_tick, events = 0, first, 0

    async def advance_to(at: datetime) -> None:
        nonlocal next_signal, next_tick
        if not engine.pending and not any(engine.positions.values()):
            next_tick = max(next_tick, at - TICK)  # nothing to manage: skip idle ticks
        while next_tick <= at:
            while next_signal < len(signals) and signals[next_signal].at <= next_tick:
                await engine.on_analysis(signals[next_signal])
                next_signal += 1
            await engine.tick(next_tick)
            next_tick += TICK
        while next_signal < len(signals) and signals[next_signal].at <= at:
            await engine.on_analysis(signals[next_signal])
            next_signal += 1

    async for message in stream_events(factory, first, stop):
        await advance_to(message.received_at)
        await engine.on_raw(message)
        events += 1
    await advance_to(stop)
    await engine.tick(stop)
    return ReplayResult(
        run_id,
        first,
        stop,
        events,
        len(signals),
        holdout_start,
        include_holdout,
        dict(engine.counters),
    )


def load_config(path: str | None) -> PaperTradingConfig:
    """Defaults, overridden by a (possibly partial) JSON file."""
    base = PaperTradingConfig().model_dump(mode="json")
    if path is None:
        return PaperTradingConfig.model_validate(base)

    def merge(into: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(into.get(key), dict):
                merge(into[key], value)
            else:
                into[key] = value
        return into

    overrides = json.loads(Path(path).read_text(encoding="utf-8"))
    return PaperTradingConfig.model_validate(merge(base, overrides))


async def _main(args: argparse.Namespace) -> None:
    from backend.config import get_settings
    from backend.database.session import create_engine, create_session_factory
    from backend.reporting.render import render_text
    from backend.reporting.report import ReportConfig
    from backend.reporting.service import generate_report

    config = load_config(args.config)
    run_id = args.run_id or f"replay-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    engine = create_engine(get_settings().database_url.get_secret_value())
    factory = create_session_factory(engine)
    try:
        try:
            result = await replay(
                factory,
                config,
                run_id=run_id,
                start=datetime.fromisoformat(args.start) if args.start else None,
                end=datetime.fromisoformat(args.end) if args.end else None,
                include_holdout=args.include_holdout,
            )
        except ValueError as exc:
            print(f"Rejeu impossible : {exc}")
            return
        print(
            f"Rejeu {result.run_id} : {result.events} événements, {result.analyses} analyses, "
            f"{result.start:%Y-%m-%d %H:%M} → {result.end:%Y-%m-%d %H:%M}"
        )
        if not result.included_holdout:
            print(f"Période réservée exclue (à partir du {result.holdout_start:%Y-%m-%d %H:%M}).")
        report = await generate_report(
            factory, ReportConfig(), run_id=run_id, start=result.start, end=result.end
        )
        if report is not None:
            print(render_text(report))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Rejeu des données enregistrées (paper)")
    parser.add_argument("--config", default=None, help="JSON partiel de PaperTradingConfig")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--include-holdout",
        action="store_true",
        help="inclure la période réservée (validation finale uniquement)",
    )
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
