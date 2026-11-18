"""Event-driven paper-trading engine.

Time only moves with the events it receives (market messages, analyses, ticks), never with the
wall clock, so a replay of recorded data reproduces exactly the same decisions and fills.

Ordering guarantees (no look-ahead):
- an order decided at t executes at t + latency, against the market state *at that moment*:
  pending orders are executed before applying any event that happens after their time;
- exit triggers use executable sale values, and the sale happens after the latency too.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from backend.database.enums import PositionStatus, TradeSide, TradeStatus
from backend.execution.paper import Fill, PaperExecutor, effective_price
from backend.monitoring.logging import get_logger
from backend.risk.manager import RiskManager, StrategyBook
from backend.scanner.messages import (
    MessageError,
    PoolMigrated,
    TokenCreated,
    TradeObserved,
    parse_message,
)
from backend.scanner.providers.base import ProviderItem, RawMessage
from backend.trading.config import PaperTradingConfig, StrategyConfig
from backend.trading.market import MarketBook, TokenMarket
from backend.trading.positions import PaperPosition, evaluate_exit
from backend.trading.repository import TradeRecord, TradingStore
from backend.trading.strategies import (
    AnalysisSignal,
    strategy_a,
    strategy_b,
    strategy_c,
)

log = get_logger(__name__)

AGGREGATOR_VALUE_REFRESH = timedelta(seconds=15)
STUCK_RETRY = timedelta(seconds=30)
B_RETRY = timedelta(minutes=5)
MARKET_IDLE_PRUNE = timedelta(hours=3)


@dataclass(slots=True)
class PendingOrder:
    execute_at: datetime
    strategy: str
    mint: str
    side: TradeSide
    amount: int  # lamports (buy) or raw tokens (sell)
    reason: str
    decided_at: datetime
    expected_out: int | None


class PaperTradingEngine:
    def __init__(
        self,
        config: PaperTradingConfig,
        executor: PaperExecutor,
        risk: RiskManager,
        store: TradingStore,
        *,
        run_id: str = "live",
    ) -> None:
        self.config = config
        self.executor = executor
        self.risk = risk
        self.store = store
        self.run_id = run_id
        self.strategies: dict[str, StrategyConfig] = {
            s.name: s for s in config.strategies if s.enabled
        }
        self.books: dict[str, StrategyBook] = {n: risk.new_book() for n in self.strategies}
        self.positions: dict[str, dict[str, PaperPosition]] = {n: {} for n in self.strategies}
        self.pending: list[PendingOrder] = []
        self.market = MarketBook()
        self.analyses: dict[str, AnalysisSignal] = {}
        self.safety: dict[str, str] = {}
        self.now: datetime | None = None
        self._config_id: int | None = None
        # Seeded per strategy: the random control (C) is reproducible in a replay.
        self._rngs = {
            n: random.Random(f"{config.seed}:{n}")  # noqa: S311 - sampling, not cryptography
            for n in self.strategies
        }
        self._aggregator_values: dict[tuple[str, str], tuple[datetime, int | None]] = {}
        self._stuck_until: dict[tuple[str, str], datetime] = {}
        self._b_attempts: dict[str, datetime] = {}
        self._last_snapshot: datetime | None = None
        self.counters: dict[str, int] = {"entries": 0, "exits": 0, "vetoes": 0, "failed_fills": 0}

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self, now: datetime) -> None:
        self._config_id = await self.store.config_version_id(self.config)
        capital = self.risk.limits.trading_capital_lamports
        for name, book in self.books.items():
            restored = await self.store.restore(self.run_id, name, now)
            book.cash = capital + restored.realized_total - restored.interrupted_cost
            book.realized = list(restored.realized)
            book.realized_total = restored.realized_total
            book.entries = [at for at, _ in restored.entries]
            book.last_entry_by_token = {mint: at for at, mint in restored.entries}
            book.peak_equity = max(capital, book.cash)
        self.now = now

    @property
    def config_id(self) -> int:
        if self._config_id is None:
            raise RuntimeError("engine not started")
        return self._config_id

    def pinned_mints(self) -> set[str]:
        """Tokens whose trade stream must stay subscribed (open positions, pending orders)."""
        mints = {m for positions in self.positions.values() for m in positions}
        return mints | {o.mint for o in self.pending}

    # --- inputs ----------------------------------------------------------------------------

    async def on_raw(self, item: ProviderItem) -> None:
        if not isinstance(item, RawMessage):
            return
        try:
            event = parse_message(item.payload)
        except MessageError:
            return
        if not isinstance(event, TokenCreated | TradeObserved | PoolMigrated):
            return
        at = item.received_at
        await self._execute_due(at)  # before applying anything that happened later
        market = self.market.apply(event, at)
        if (
            isinstance(event, TradeObserved)
            and event.side is TradeSide.SELL
            and market.creator is not None
            and event.trader == market.creator
        ):
            self.safety[event.mint] = "creator_sold"
        self._set_now(at)
        await self._evaluate_exits(at, only=event.mint)
        if "B" in self.strategies and market.migrated:
            await self._consider_b(market, at)

    async def on_analysis(self, signal: AnalysisSignal) -> None:
        await self._execute_due(signal.at)
        self._set_now(signal.at)
        self.analyses[signal.mint] = signal
        if signal.critical_flags:
            self.safety[signal.mint] = f"critical:{signal.critical_flags[0]}"
            await self._evaluate_exits(signal.at, only=signal.mint)
            return
        if "A" in self.strategies:
            entry = strategy_a(self.strategies["A"], signal)
            if entry is not None:
                await self._try_enter(
                    "A", signal.mint, entry.reason, signal.at, signal.entry_impact
                )
        if "C" in self.strategies:
            entry = strategy_c(self.strategies["C"], signal, self._rngs["C"])
            if entry is not None:
                await self._try_enter(
                    "C", signal.mint, entry.reason, signal.at, signal.entry_impact
                )

    async def tick(self, now: datetime) -> None:
        await self._execute_due(now)
        self._set_now(now)
        await self._evaluate_exits(now)
        interval = timedelta(minutes=self.config.snapshot_interval_minutes)
        if self._last_snapshot is None or now - self._last_snapshot >= interval:
            await self.snapshot(now)
            self._prune(now)

    def _set_now(self, at: datetime) -> None:
        self.now = at if self.now is None else max(self.now, at)

    # --- entries ---------------------------------------------------------------------------

    async def _record(
        self,
        agent: str,
        mint: str,
        decision: str,
        reason: str,
        at: datetime,
        payload: dict[str, Any],
    ) -> None:
        await self.store.record_decision(agent, mint, decision, reason, payload, at, self.config_id)

    async def _try_enter(
        self, strategy: str, mint: str, reason: str, at: datetime, impact: Decimal | None
    ) -> None:
        book = self.books[strategy]
        decision = self.risk.check_entry(book, mint, at, impact)
        payload = {
            "strategy": strategy,
            "signal": reason,
            "entry_impact": None if impact is None else str(impact),
        }
        await self._record("strategist", mint, "enter", reason, at, payload)
        if not decision.approved:
            self.counters["vetoes"] += 1
            await self._record("risk_manager", mint, "veto", decision.reason, at, payload)
            return
        await self._record(
            "risk_manager",
            mint,
            "approve",
            f"size={decision.size_lamports}",
            at,
            payload | {"size_lamports": decision.size_lamports},
        )
        book.entries.append(at)
        book.last_entry_by_token[mint] = at
        book.open_cost[mint] = decision.size_lamports  # budget reserved before the fill
        market = self.market.ensure(mint)
        expected = self.executor.expected_buy(market, decision.size_lamports)
        self.pending.append(
            PendingOrder(
                at + timedelta(milliseconds=self.config.execution.latency_ms),
                strategy,
                mint,
                TradeSide.BUY,
                decision.size_lamports,
                reason,
                at,
                expected,
            )
        )

    async def _consider_b(self, market: TokenMarket, at: datetime) -> None:
        last = self._b_attempts.get(market.mint)
        if last is not None and at - last < B_RETRY:
            return
        signal = strategy_b(self.strategies["B"], market, self.analyses.get(market.mint), at)
        if signal is None:
            return
        self._b_attempts[market.mint] = at
        if not self.executor.has_aggregator:
            await self._record(
                "strategist",
                market.mint,
                "skip",
                "no_post_migration_quotes",
                at,
                {"strategy": "B", "signal": signal.reason},
            )
            return
        impact = await self.executor.aggregator_buy_impact(
            market.mint, self.risk.limits.max_position_lamports
        )
        await self._try_enter("B", market.mint, signal.reason, at, impact)

    # --- order execution -------------------------------------------------------------------

    async def _execute_due(self, now: datetime) -> None:
        due = sorted((o for o in self.pending if o.execute_at <= now), key=lambda o: o.execute_at)
        if not due:
            return
        self.pending = [o for o in self.pending if o.execute_at > now]
        for order in due:
            market = self.market.ensure(order.mint)
            if order.side is TradeSide.BUY:
                await self._fill_buy(order, market)
            else:
                await self._fill_sell(order, market)

    def _trade(
        self,
        key: str,
        side: TradeSide,
        fill: Fill,
        at: datetime,
        *,
        rent: int,
        expected: int | None,
    ) -> TradeRecord:
        sol = fill.in_amount if side is TradeSide.BUY else fill.out_amount
        tokens = fill.out_amount if side is TradeSide.BUY else fill.in_amount
        return TradeRecord(
            key=key,
            side=side,
            status=TradeStatus.CONFIRMED if fill.ok else TradeStatus.FAILED,
            quantity=tokens if fill.ok else (fill.in_amount if side is TradeSide.SELL else 0),
            sol_amount_lamports=sol if fill.ok else None,
            price=effective_price(sol, tokens) if fill.ok else None,
            fees_lamports=fill.fees_lamports,
            network_fee_lamports=fill.network_fee_lamports,
            rent_lamports=rent,
            fill_model=fill.fill_model,
            expected_out=expected,
            actual_out=fill.out_amount if fill.ok else None,
            failure_reason=fill.failure_reason,
            at=at,
        )

    async def _fill_buy(self, order: PendingOrder, market: TokenMarket) -> None:
        book, at = self.books[order.strategy], order.execute_at
        fill = await self.executor.buy(market, order.amount)
        if not fill.ok:
            self.counters["failed_fills"] += 1
            book.open_cost.pop(order.mint, None)
            book.cash -= fill.network_fee_lamports
            failed = PaperPosition(
                order.strategy,
                order.mint,
                at,
                fill.network_fee_lamports,
                0,
                0,
                Decimal(0),
                exit_reason=f"entry_failed:{fill.failure_reason}",
            )
            failed.db_id = await self.store.open_position(failed, self.config_id, self.run_id)
            await self.store.record_trade(
                failed.db_id,
                self._trade(
                    failed.next_trade_key(self.run_id),
                    TradeSide.BUY,
                    fill,
                    at,
                    rent=0,
                    expected=order.expected_out,
                ),
            )
            pnl = -fill.network_fee_lamports
            book.realized.append((at, pnl))
            book.realized_total += pnl
            await self.store.close_position(failed, pnl, at, PositionStatus.CLOSED)
            return
        rent = self.config.execution.token_account_rent_lamports
        cost = order.amount + fill.network_fee_lamports + rent
        book.cash -= cost
        book.open_cost[order.mint] = cost
        price = effective_price(order.amount, fill.out_amount) or Decimal(0)
        pos = PaperPosition(
            order.strategy, order.mint, at, cost, fill.out_amount, fill.out_amount, price
        )
        pos.db_id = await self.store.open_position(pos, self.config_id, self.run_id)
        await self.store.record_trade(
            pos.db_id,
            self._trade(
                pos.next_trade_key(self.run_id),
                TradeSide.BUY,
                fill,
                at,
                rent=rent,
                expected=order.expected_out,
            ),
        )
        self.positions[order.strategy][order.mint] = pos
        self.counters["entries"] += 1

    async def _fill_sell(self, order: PendingOrder, market: TokenMarket) -> None:
        pos = self.positions[order.strategy].get(order.mint)
        if pos is None or pos.db_id is None:
            return
        book, at, key = self.books[order.strategy], order.execute_at, (order.strategy, order.mint)
        quantity = min(order.amount, pos.remaining)
        fill = await self.executor.sell(market, quantity)
        pos.selling = False
        if not fill.ok:
            self.counters["failed_fills"] += 1
            book.cash -= fill.network_fee_lamports
            pos.cost_lamports += fill.network_fee_lamports  # a failed attempt is a real cost
            if fill.failure_reason == "no_executable_quote":
                if key in self._stuck_until:
                    self._stuck_until[key] = at + STUCK_RETRY
                    return  # already reported: retry silently
                self._stuck_until[key] = at + STUCK_RETRY
            await self.store.record_trade(
                pos.db_id,
                self._trade(
                    pos.next_trade_key(self.run_id),
                    TradeSide.SELL,
                    fill,
                    at,
                    rent=0,
                    expected=order.expected_out,
                ),
            )
            await self.store.update_position(pos)
            return
        self._stuck_until.pop(key, None)
        final = quantity >= pos.remaining
        rent = self.config.execution.token_account_rent_lamports if final else 0
        proceeds = fill.out_amount - fill.network_fee_lamports + rent
        pos.remaining -= quantity
        pos.sold_quantity += quantity
        pos.proceeds_lamports += proceeds
        book.cash += proceeds
        if order.reason == "take_profit_1":
            pos.tp1_done = True
        elif order.reason == "take_profit_2":
            pos.tp2_done = True
        await self.store.record_trade(
            pos.db_id,
            self._trade(
                pos.next_trade_key(self.run_id),
                TradeSide.SELL,
                fill,
                at,
                rent=-rent,
                expected=order.expected_out,
            ),
        )
        if final:
            pnl = pos.proceeds_lamports - pos.cost_lamports
            pos.exit_reason = order.reason
            book.record_close(at, pnl, self.risk.limits)
            book.realized_total += pnl
            book.open_cost.pop(order.mint, None)
            del self.positions[order.strategy][order.mint]
            self._aggregator_values.pop(key, None)
            await self.store.close_position(pos, pnl, at, PositionStatus.CLOSED)
            self.counters["exits"] += 1
        else:
            book.open_cost[order.mint] = int(pos.cost_basis_remaining())
            await self.store.update_position(pos)

    # --- exits -----------------------------------------------------------------------------

    async def _value(
        self, strategy: str, pos: PaperPosition, market: TokenMarket, now: datetime
    ) -> int | None:
        if not market.migrated:
            return self.executor.value_of(market, pos.remaining)
        if not self.executor.has_aggregator:
            return None
        key = (strategy, pos.mint)
        cached = self._aggregator_values.get(key)
        if cached is not None and now - cached[0] < AGGREGATOR_VALUE_REFRESH:
            return cached[1]
        value = await self.executor.aggregator_value(pos.mint, pos.remaining)
        self._aggregator_values[key] = (now, value)
        return value

    def _sell_pressure(self, market: TokenMarket, strategy: str, now: datetime) -> bool:
        rules = self.strategies[strategy].exits
        bought, sold = market.flow(now, timedelta(seconds=rules.sell_pressure_window_seconds))
        return sold >= rules.sell_pressure_min_lamports and Decimal(sold) >= (
            rules.sell_pressure_ratio * Decimal(bought)
        )

    async def _evaluate_exits(self, now: datetime, only: str | None = None) -> None:
        for strategy, positions in self.positions.items():
            for mint, pos in list(positions.items()):
                if only is not None and mint != only:
                    continue
                stuck = self._stuck_until.get((strategy, mint))
                if stuck is not None and now < stuck:
                    continue
                market = self.market.ensure(mint)
                safety = self.safety.get(mint)
                if safety is None and self._sell_pressure(market, strategy, now):
                    safety = "sell_pressure"
                value = await self._value(strategy, pos, market, now)
                decision = evaluate_exit(pos, self.strategies[strategy].exits, now, value, safety)
                if decision is None:
                    continue
                pos.selling = True
                expected = self.executor.expected_sell(market, decision.quantity)
                self.pending.append(
                    PendingOrder(
                        now + timedelta(milliseconds=self.config.execution.latency_ms),
                        strategy,
                        mint,
                        TradeSide.SELL,
                        decision.quantity,
                        decision.reason,
                        now,
                        expected,
                    )
                )

    # --- reporting -------------------------------------------------------------------------

    async def snapshot(self, now: datetime) -> None:
        self._last_snapshot = now
        for strategy, book in self.books.items():
            values = []
            unrealized = 0
            for mint, pos in self.positions[strategy].items():
                value = await self._value(strategy, pos, self.market.ensure(mint), now)
                values.append(value or 0)  # unknown value counted as zero (conservative)
                unrealized += (value or 0) - int(pos.cost_basis_remaining())
            equity = book.cash + sum(values)
            book.peak_equity = max(book.peak_equity, equity)
            drawdown = (
                Decimal(book.peak_equity - equity) / Decimal(book.peak_equity)
                if book.peak_equity > 0
                else Decimal(0)
            )
            await self.store.snapshot(
                self.run_id,
                strategy,
                now,
                {
                    "total_equity_lamports": equity,
                    "available_lamports": book.cash,
                    "reserved_lamports": sum(book.open_cost.values()),
                    "realized_pnl_lamports": book.realized_total,
                    "unrealized_pnl_lamports": unrealized,
                    "drawdown": drawdown.quantize(Decimal("0.000001")),
                },
            )

    def _prune(self, now: datetime) -> None:
        keep = self.pinned_mints()
        for mint in list(self.analyses):
            if now - self.analyses[mint].at > MARKET_IDLE_PRUNE and mint not in keep:
                del self.analyses[mint]
                self.safety.pop(mint, None)
        self.market.prune_idle(now, MARKET_IDLE_PRUNE, keep)
