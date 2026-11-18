"""End-to-end behaviour of the paper engine on scripted market events (no database)."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.database.enums import TradeSide, TradeStatus
from backend.execution.paper import PaperExecutor
from backend.risk.manager import RiskManager
from backend.trading.config import ExecutionModel, PaperTradingConfig, StrategyConfig
from backend.trading.engine import PaperTradingEngine
from backend.trading.repository import RestoredStrategy
from tests.fixtures.pumpportal import mint
from tests.fixtures.trading import MemoryStore, a_signal, created, migrated, seconds, traded

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
TOKEN, CREATOR = mint(1), mint(9)


def engine(
    *,
    fail: bool = False,
    kill: bool = False,
    strategies: tuple[str, ...] = ("A",),
) -> tuple[PaperTradingEngine, MemoryStore]:
    rate = Decimal(1) if fail else Decimal(0)
    config = PaperTradingConfig(
        strategies=tuple(StrategyConfig(name=n) for n in strategies),
        execution=ExecutionModel(buy_failure_rate=rate, sell_failure_rate=rate),
    )
    store = MemoryStore()
    risk = RiskManager(config.risk, lambda: kill, lambda _now: True)
    executor = PaperExecutor(config.execution, random.Random(1))
    return PaperTradingEngine(config, executor, risk, store), store


async def open_a_position(eng: PaperTradingEngine) -> None:
    await eng.start(T0)
    await eng.on_raw(created(TOKEN, T0, CREATOR))
    await eng.on_analysis(a_signal(TOKEN, seconds(T0, 180)))
    await eng.on_raw(traded(TOKEN, seconds(T0, 182), 1.0, seq=1))  # after the 1.5 s latency


def sells(store: MemoryStore) -> list[tuple[str | None, int]]:
    return [(t.failure_reason, t.quantity) for _, t in store.trades if t.side is TradeSide.SELL]


async def test_full_lifecycle_take_profits_then_trailing_stop() -> None:
    eng, store = engine()
    await open_a_position(eng)
    pos = eng.positions["A"][TOKEN]
    cost = pos.cost_lamports
    # Price x1.5 -> TP1 (40 %), x2.0 -> TP2 (30 %), peak x2.4 then fall -> trailing on the rest.
    script = [(190, 1.5), (195, 1.5), (200, 2.0), (205, 2.0), (210, 2.4), (215, 1.7), (220, 1.7)]
    for i, (s, multiple) in enumerate(script):
        await eng.on_raw(traded(TOKEN, seconds(T0, s), multiple, seq=10 + i))
    closed = next(p for p in store.positions.values() if p["status"] == "closed")
    assert closed["reason"] == "trailing_stop"
    reasons = [d[3] for d in store.decisions if d[0] == "risk_manager"]
    assert reasons[0].startswith("size=")
    quantities = [q for _, q in sells(store)]
    assert quantities[0] == int(pos.quantity * Decimal("0.4"))
    assert sum(quantities) == pos.quantity
    # Accounting: PnL is exactly what came back minus everything spent, and cash agrees.
    proceeds = sum(t.sol_amount_lamports or 0 for _, t in store.trades if t.side is TradeSide.SELL)
    fees = sum(t.network_fee_lamports for _, t in store.trades if t.side is TradeSide.SELL)
    rent = eng.config.execution.token_account_rent_lamports
    assert closed["pnl"] == proceeds - fees + rent - cost
    assert eng.books["A"].cash == eng.risk.limits.trading_capital_lamports + closed["pnl"]
    assert closed["pnl"] > 0


async def test_stop_loss_executes_after_latency_at_the_later_price() -> None:
    eng, store = engine()
    await open_a_position(eng)
    await eng.on_raw(traded(TOKEN, seconds(T0, 200), 0.80, seq=20))  # -20 %: stop triggered
    await eng.on_raw(traded(TOKEN, seconds(T0, 201), 0.60, seq=21))  # crash during the latency
    await eng.on_raw(traded(TOKEN, seconds(T0, 205), 0.60, seq=22))
    closed = next(p for p in store.positions.values() if p["status"] == "closed")
    assert closed["reason"] == "stop_loss"
    assert closed["pnl"] < -int(closed["cost"] * Decimal("0.30"))  # worse than the -15 % trigger


async def test_creator_sale_forces_safety_exit() -> None:
    eng, store = engine()
    await open_a_position(eng)
    await eng.on_raw(traded(TOKEN, seconds(T0, 190), 1.1, side="sell", trader=CREATOR, seq=30))
    await eng.on_raw(traded(TOKEN, seconds(T0, 195), 1.1, seq=31))
    closed = next(p for p in store.positions.values() if p["status"] == "closed")
    assert closed["reason"] == "creator_sold"


async def test_kill_switch_vetoes_entries() -> None:
    eng, store = engine(kill=True)
    await open_a_position(eng)
    assert eng.positions["A"] == {}
    assert ("risk_manager", TOKEN, "veto", "kill_switch_active") in store.decisions


async def test_failed_entry_is_a_recorded_cost() -> None:
    eng, store = engine(fail=True)
    await open_a_position(eng)
    (failed,) = store.positions.values()
    assert failed["status"] == "closed" and failed["pnl"] == -55_000
    assert failed["reason"] == "entry_failed:simulated_failure"
    assert store.trades[0][1].status is TradeStatus.FAILED
    assert eng.books["A"].cash == eng.risk.limits.trading_capital_lamports - 55_000
    assert eng.books["A"].open_cost == {}


async def test_graduation_without_aggregator_leaves_only_time_exit() -> None:
    eng, store = engine()
    await open_a_position(eng)
    await eng.on_raw(migrated(TOKEN, seconds(T0, 190)))
    await eng.tick(seconds(T0, 300))
    assert TOKEN in eng.positions["A"]  # no executable price: no stop/TP possible
    await eng.tick(seconds(T0, 180 + 21 * 60))  # max hold reached: sale attempted
    await eng.tick(seconds(T0, 180 + 21 * 60 + 5))
    await eng.tick(seconds(T0, 180 + 21 * 60 + 60))
    assert sells(store) == [("no_executable_quote", eng.positions["A"][TOKEN].remaining)]
    assert TOKEN in eng.pinned_mints()


async def test_restart_restores_capital_and_limits() -> None:
    eng, store = engine()
    store.restored["A"] = RestoredStrategy(
        realized=[(T0 - timedelta(hours=1), -50_000_000)],
        entries=[(T0 - timedelta(hours=1), TOKEN)],
        realized_total=-50_000_000,
        interrupted_cost=20_000_000,
    )
    await eng.start(T0)
    book = eng.books["A"]
    assert book.cash == eng.risk.limits.trading_capital_lamports - 70_000_000
    decision = eng.risk.check_entry(book, TOKEN, T0, Decimal("0.02"))
    assert decision.reason == "token_cooldown"


async def test_replay_is_deterministic() -> None:
    async def run() -> list[tuple[str | None, int]]:
        eng, store = engine(strategies=("A", "C"))
        await open_a_position(eng)
        for i, multiple in enumerate((1.3, 1.6, 1.2, 0.9, 0.8)):
            await eng.on_raw(traded(TOKEN, seconds(T0, 200 + 10 * i), multiple, seq=40 + i))
        await eng.tick(seconds(T0, 3000))
        await eng.tick(seconds(T0, 3005))
        return [(t.fill_model, t.quantity) for _, t in store.trades]

    assert await run() == await run()


async def test_snapshots_value_open_positions() -> None:
    eng, store = engine()
    await open_a_position(eng)
    await eng.tick(seconds(T0, 185))
    strategy, _, values = store.snapshots[-1]
    assert strategy == "A"
    assert values["reserved_lamports"] > 0
    assert values["total_equity_lamports"] < eng.risk.limits.trading_capital_lamports  # costs
