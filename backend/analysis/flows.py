"""Trade-flow features computed from the trades recorded by the scanner (no RPC cost)."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.enums import TradeSide
from backend.database.models import RawEvent
from backend.scanner.messages import MessageError, TradeObserved, parse_message

Q = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class TradeFlow:
    n_buys: int = 0
    n_sells: int = 0
    unique_buyers: int = 0
    unique_sellers: int = 0
    buy_lamports: int = 0
    sell_lamports: int = 0
    buy_ratio: Decimal | None = None  # SOL bought / (bought + sold)
    acceleration: Decimal | None = None  # recent buys per minute / previous buys per minute
    wash_ratio: Decimal | None = None  # wallets that both bought and sold / unique traders
    sellers: frozenset[str] = field(default_factory=frozenset)

    def as_metrics(self) -> dict[str, Any]:
        return {
            "n_buys": self.n_buys,
            "n_sells": self.n_sells,
            "unique_buyers": self.unique_buyers,
            "unique_sellers": self.unique_sellers,
            "buy_lamports": self.buy_lamports,
            "sell_lamports": self.sell_lamports,
            "buy_ratio": None if self.buy_ratio is None else str(self.buy_ratio),
            "acceleration": None if self.acceleration is None else str(self.acceleration),
            "wash_ratio": None if self.wash_ratio is None else str(self.wash_ratio),
        }


def compute_trade_flow(
    trades: list[tuple[datetime, TradeObserved]],
    now: datetime,
    recent: timedelta,
    previous: timedelta,
) -> TradeFlow:
    buys = [(t, e) for t, e in trades if e.side is TradeSide.BUY]
    sells = [(t, e) for t, e in trades if e.side is TradeSide.SELL]
    buyers = {e.trader for _, e in buys if e.trader}
    sellers = {e.trader for _, e in sells if e.trader}
    buy_sol = sum(e.sol_amount_lamports or 0 for _, e in buys)
    sell_sol = sum(e.sol_amount_lamports or 0 for _, e in sells)
    total_sol = buy_sol + sell_sol
    traders = buyers | sellers

    recent_start = now - recent
    previous_start = recent_start - previous
    recent_buys = sum(1 for t, _ in buys if recent_start <= t <= now)
    previous_buys = sum(1 for t, _ in buys if previous_start <= t < recent_start)
    acceleration = None
    if previous_buys > 0:
        recent_rate = Decimal(recent_buys) / Decimal(recent.total_seconds() / 60)
        previous_rate = Decimal(previous_buys) / Decimal(previous.total_seconds() / 60)
        acceleration = (recent_rate / previous_rate).quantize(Q)

    return TradeFlow(
        n_buys=len(buys),
        n_sells=len(sells),
        unique_buyers=len(buyers),
        unique_sellers=len(sellers),
        buy_lamports=buy_sol,
        sell_lamports=sell_sol,
        buy_ratio=(Decimal(buy_sol) / Decimal(total_sol)).quantize(Q) if total_sol else None,
        acceleration=acceleration,
        wash_ratio=(Decimal(len(buyers & sellers)) / Decimal(len(traders))).quantize(Q)
        if traders
        else None,
        sellers=frozenset(sellers),
    )


async def load_trades(
    factory: async_sessionmaker[AsyncSession], mint: str, until: datetime
) -> list[tuple[datetime, TradeObserved]]:
    """Recorded trades of one token up to ``until`` (never look into the future)."""
    async with factory() as session:
        rows = await session.execute(
            select(RawEvent.received_at, RawEvent.payload)
            .where(
                RawEvent.event_type == "trade",
                RawEvent.payload["mint"].astext == mint,
                RawEvent.received_at <= until,
            )
            .order_by(RawEvent.received_at)
        )
        result: list[tuple[datetime, TradeObserved]] = []
        for received_at, payload in rows.all():
            try:
                event = parse_message(payload)
            except MessageError:
                continue
            if isinstance(event, TradeObserved):
                result.append((received_at, event))
    return result
