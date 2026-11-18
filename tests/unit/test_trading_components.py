import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.blockchain.curve_math import buy_tokens_out, sell_sol_out
from backend.database.enums import TradeSide
from backend.execution.paper import PaperExecutor, effective_price
from backend.risk.manager import RiskManager
from backend.scanner.messages import parse_message
from backend.trading.config import ExecutionModel, ExitRules, RiskLimits, StrategyConfig
from backend.trading.market import MarketBook, TokenMarket, implied_price
from backend.trading.positions import PaperPosition, evaluate_exit
from backend.trading.strategies import strategy_a, strategy_b, strategy_c
from tests.fixtures.pumpportal import mint
from tests.fixtures.trading import FakeJupiter, a_signal, created, migrated, reserves, traded

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
NO_FAIL = ExecutionModel(buy_failure_rate=Decimal(0), sell_failure_rate=Decimal(0))


def market_at(multiple: float = 1) -> TokenMarket:
    v_sol, v_tokens = reserves(multiple)
    return TokenMarket(mint(1), v_sol=v_sol, v_tokens=v_tokens, updated_at=T0)


def apply(book: MarketBook, msg: object) -> TokenMarket:
    event = parse_message(msg.payload)  # type: ignore[attr-defined]
    return book.apply(event, msg.received_at)  # type: ignore[arg-type, attr-defined]


# --- market ------------------------------------------------------------------------------


def test_market_book_tracks_reserves_flow_and_migration() -> None:
    book = MarketBook()
    apply(book, created(mint(1), T0, mint(9)))
    m = apply(book, traded(mint(1), T0 + timedelta(seconds=10), 1.2, sol=1.0, seq=1))
    apply(book, traded(mint(1), T0 + timedelta(seconds=20), 1.1, side="sell", sol=0.4, seq=2))
    assert m.creator == mint(9)
    assert (m.v_sol, m.v_tokens) == reserves(1.1)
    assert m.flow(T0 + timedelta(seconds=30), timedelta(seconds=60)) == (10**9, 4 * 10**8)
    apply(book, migrated(mint(1), T0 + timedelta(seconds=40)))
    assert m.migrated and m.v_sol is None  # curve no longer executable
    book.prune_idle(T0 + timedelta(hours=5), timedelta(hours=3), keep=set())
    assert book.get(mint(1)) is None


def test_implied_price() -> None:
    assert implied_price(10**9, 10**12) == Decimal("0.000001")  # 1 SOL for 1e6 tokens
    assert implied_price(0, 10) is None


# --- execution ---------------------------------------------------------------------------


async def test_curve_buy_applies_fees_and_haircut() -> None:
    market = market_at()
    fill = await PaperExecutor(NO_FAIL, random.Random(0)).buy(market, 70_000_000)
    raw = buy_tokens_out(market.v_sol or 0, market.v_tokens or 0, 70_000_000, 125)
    expected = raw * 9950 // 10000 * 9900 // 10000
    assert fill.ok and fill.out_amount == expected and fill.fill_model == "curve"
    assert fill.network_fee_lamports == 55_000


async def test_worst_quote_wins() -> None:
    market = market_at()
    generous = FakeJupiter(Decimal(10**9))  # far better than the curve: must be ignored
    fill = await PaperExecutor(NO_FAIL, random.Random(0), generous).buy(market, 70_000_000)
    assert fill.fill_model == "worst_of" and fill.out_amount < 70_000_000 * 10**9
    stingy = FakeJupiter(Decimal("0.0001"))
    poor = await PaperExecutor(NO_FAIL, random.Random(0), stingy).buy(market, 70_000_000)
    assert poor.out_amount == 7_000 * 9900 // 10000


async def test_failures_cost_network_fees_except_without_quote() -> None:
    always_fail = ExecutionModel(buy_failure_rate=Decimal(1), sell_failure_rate=Decimal(1))
    ex = PaperExecutor(always_fail, random.Random(0))
    failed = await ex.buy(market_at(), 10**7)
    assert not failed.ok and failed.failure_reason == "simulated_failure"
    assert failed.network_fee_lamports == 55_000
    no_quote = await ex.buy(TokenMarket(mint(2)), 10**7)
    assert no_quote.failure_reason == "no_executable_quote" and no_quote.network_fee_lamports == 0


async def test_sell_and_mark_to_market() -> None:
    market = market_at()
    ex = PaperExecutor(NO_FAIL, random.Random(0))
    fill = await ex.sell(market, 10**12)
    raw = sell_sol_out(market.v_sol or 0, market.v_tokens or 0, 10**12, 125)
    assert fill.out_amount == raw * 9950 // 10000 * 9900 // 10000
    assert ex.value_of(market, 10**12) == fill.out_amount - 55_000
    market.migrated_at = T0
    assert ex.value_of(market, 10**12) is None


async def test_aggregator_impact_and_value() -> None:
    ex = PaperExecutor(NO_FAIL, random.Random(0), FakeJupiter(Decimal(1000)))
    assert await ex.aggregator_buy_impact(mint(1), 10**8) == Decimal(0)
    # 1e6 raw tokens -> 1000 lamports, minus haircut and network fee: worthless, floored at 0.
    assert await ex.aggregator_value(mint(1), 10**6) == 0
    assert await ex.aggregator_value(mint(1), 10**12) == 10**9 * 9900 // 10000 - 55_000
    assert await PaperExecutor(NO_FAIL, random.Random(0)).aggregator_value(mint(1), 1) is None


def test_effective_price() -> None:
    assert effective_price(10**9, 10**12) == Decimal("0.000001")
    assert effective_price(0, 1) is None


# --- risk --------------------------------------------------------------------------------


def risk(kill: bool = False, feed: bool = True, **limits: object) -> RiskManager:
    return RiskManager(RiskLimits(**limits), lambda: kill, lambda _now: feed)


def test_risk_vetoes() -> None:
    impact = Decimal("0.02")
    assert risk(kill=True).check_entry(risk().new_book(), mint(1), T0, impact).reason == (
        "kill_switch_active"
    )
    assert risk(feed=False).check_entry(risk().new_book(), mint(1), T0, impact).reason == (
        "feed_unhealthy"
    )
    rm = risk()
    book = rm.new_book()
    assert rm.check_entry(book, mint(1), T0, None).reason == "no_cost_estimate"
    assert rm.check_entry(book, mint(1), T0, Decimal("0.2")).reason == "price_impact_too_high"
    book.open_cost = {mint(2): 1, mint(3): 1}
    assert rm.check_entry(book, mint(1), T0, impact).reason == "max_open_positions"
    book.open_cost = {}
    book.last_entry_by_token[mint(1)] = T0 - timedelta(hours=1)
    assert rm.check_entry(book, mint(1), T0, impact).reason == "token_cooldown"
    book.entries = [T0] * 10
    assert rm.check_entry(book, mint(4), T0, impact).reason == "max_entries_per_day"


def test_position_size_never_exceeds_loss_budget() -> None:
    rm = risk()
    book = rm.new_book()
    ok = rm.check_entry(book, mint(1), T0, Decimal("0.02"))
    assert ok.approved and ok.size_lamports == 60_000_000  # daily budget 3 % of 2 SOL < 0.07
    book.open_cost[mint(9)] = 45_000_000  # an open position counts as possibly lost
    small = rm.check_entry(book, mint(1), T0, Decimal("0.02"))
    assert small.approved and small.size_lamports == 15_000_000
    book.realized.append((T0, -10_000_000))
    assert rm.check_entry(book, mint(1), T0, Decimal("0.02")).reason == (
        "daily_loss_budget_exhausted"
    )


def test_loss_streak_pause() -> None:
    rm = risk()
    book = rm.new_book()
    for i in range(3):
        book.record_close(T0 + timedelta(minutes=i), -1, rm.limits)
    assert (
        rm.check_entry(book, mint(1), T0 + timedelta(minutes=30), Decimal("0.02")).reason
        == "loss_streak_pause"
    )
    assert rm.check_entry(book, mint(1), T0 + timedelta(minutes=63), Decimal("0.02")).approved


# --- exits -------------------------------------------------------------------------------


def position(cost: int = 1_000_000, quantity: int = 1000) -> PaperPosition:
    return PaperPosition("A", mint(1), T0, cost, quantity, quantity, Decimal(1))


@pytest.mark.parametrize(
    ("value", "reason", "quantity"),
    [(840_000, "stop_loss", 1000), (1_260_000, "take_profit_1", 400), (1_100_000, None, 0)],
)
def test_price_exits(value: int, reason: str | None, quantity: int) -> None:
    decision = evaluate_exit(position(), ExitRules(), T0 + timedelta(minutes=1), value)
    if reason is None:
        assert decision is None
    else:
        assert decision is not None
        assert (decision.reason, decision.quantity) == (reason, quantity)


def test_take_profit_two_and_trailing_after_tp1() -> None:
    rules, pos = ExitRules(), position()
    pos.tp1_done, pos.remaining = True, 600
    tp2 = evaluate_exit(pos, rules, T0, 900_000)  # 600/1000 of cost = 600k -> +50 %
    assert tp2 is not None and (tp2.reason, tp2.quantity) == ("take_profit_2", 300)
    pos.tp2_done = True
    assert evaluate_exit(pos, rules, T0, 960_000) is None  # new peak +60 %
    trail = evaluate_exit(pos, rules, T0, 760_000)  # 1.2667 <= 1.6 * 0.8
    assert trail is not None and trail.reason == "trailing_stop"


def test_time_and_safety_exits() -> None:
    rules = ExitRules()
    assert evaluate_exit(position(), rules, T0 + timedelta(minutes=11), 1_020_000).reason == (  # type: ignore[union-attr]
        "stagnation"
    )
    assert evaluate_exit(position(), rules, T0 + timedelta(minutes=21), None).reason == (  # type: ignore[union-attr]
        "max_hold_time"
    )
    assert evaluate_exit(position(), rules, T0 + timedelta(minutes=5), None) is None
    assert evaluate_exit(position(), rules, T0, 2_000_000, "creator_sold").reason == (  # type: ignore[union-attr]
        "creator_sold"
    )
    busy = position()
    busy.selling = True
    assert evaluate_exit(busy, rules, T0, 100, "creator_sold") is None


# --- strategies --------------------------------------------------------------------------


def test_strategy_a_conditions() -> None:
    cfg = StrategyConfig(name="A")
    assert strategy_a(cfg, a_signal(mint(1), T0)) is not None
    assert strategy_a(cfg, a_signal(mint(1), T0, decision="watch")) is None
    assert strategy_a(cfg, a_signal(mint(1), T0, critical_flags=("x",))) is None
    late = a_signal(mint(1), T0, metrics={"curve_tokens_sold_share": "0.9", "unique_buyers": 55})
    assert strategy_a(cfg, late) is None
    thin = a_signal(mint(1), T0, metrics={"curve_tokens_sold_share": "0.4", "unique_buyers": 10})
    assert strategy_a(cfg, thin) is None


def test_strategy_c_is_reproducible_random_control() -> None:
    cfg = StrategyConfig(name="C")
    rng_a, rng_b = random.Random(42), random.Random(42)
    a = [strategy_c(cfg, a_signal(mint(i), T0), rng_a) is not None for i in range(200)]
    b = [strategy_c(cfg, a_signal(mint(i), T0), rng_b) is not None for i in range(200)]
    assert a == b and 5 < sum(a) < 40  # ~10 % of 200
    assert strategy_c(cfg, a_signal(mint(1), T0, critical_flags=("x",)), random.Random(0)) is None


def test_strategy_b_needs_drawdown_then_rebound_with_buyers() -> None:
    cfg = StrategyConfig(name="B")
    m = TokenMarket(mint(1), migrated_at=T0)
    prices = [
        Decimal(p)
        for p in ("1.0", "1.2", "1.0", "0.8", "0.7", "0.72", "0.74", "0.76", "0.78", "0.80")
    ]
    for i, p in enumerate(prices):
        m.implied_prices.append((T0 + timedelta(seconds=10 * i), p))
    m.trades.append((T0 + timedelta(seconds=95), TradeSide.BUY, 10**9, None))
    now = T0 + timedelta(seconds=100)
    assert strategy_b(cfg, m, a_signal(mint(1), T0), now) is not None
    assert strategy_b(cfg, m, None, now) is None  # never without a security analysis
    assert strategy_b(cfg, m, a_signal(mint(1), T0, critical_flags=("x",)), now) is None
    assert strategy_b(cfg, m, a_signal(mint(1), T0), T0 + timedelta(hours=3)) is None
