"""Latest known market state per token, rebuilt from the recorded event stream.

Only executable information is used for fills (curve reserves, or an aggregator quote).
Trade-implied prices are kept for *signals* only (e.g. strategy B's drawdown detection),
never as proof that an order would have filled at that price.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from backend.database.enums import TradeSide
from backend.scanner.messages import PUMP_TOKEN_DECIMALS, PoolMigrated, TokenCreated, TradeObserved

TRADE_HISTORY = 500


@dataclass(slots=True)
class TokenMarket:
    mint: str
    creator: str | None = None
    v_sol: int | None = None
    v_tokens: int | None = None
    updated_at: datetime | None = None
    migrated_at: datetime | None = None
    trades: deque[tuple[datetime, TradeSide, int, str | None]] = field(
        default_factory=lambda: deque(maxlen=TRADE_HISTORY)
    )
    implied_prices: deque[tuple[datetime, Decimal]] = field(
        default_factory=lambda: deque(maxlen=TRADE_HISTORY)
    )

    @property
    def migrated(self) -> bool:
        return self.migrated_at is not None

    def flow(self, now: datetime, window: timedelta) -> tuple[int, int]:
        """SOL bought and sold (lamports) during the window ending at ``now``."""
        start = now - window
        bought = sum(
            lam for t, side, lam, _ in self.trades if start <= t <= now and side is TradeSide.BUY
        )
        sold = sum(
            lam for t, side, lam, _ in self.trades if start <= t <= now and side is TradeSide.SELL
        )
        return bought, sold


def implied_price(sol_lamports: int | None, token_raw: int | None) -> Decimal | None:
    if not sol_lamports or not token_raw or sol_lamports <= 0 or token_raw <= 0:
        return None
    return (Decimal(sol_lamports) / Decimal(10**9)) / (
        Decimal(token_raw) / Decimal(10**PUMP_TOKEN_DECIMALS)
    )


class MarketBook:
    def __init__(self) -> None:
        self._tokens: dict[str, TokenMarket] = {}

    def get(self, mint: str) -> TokenMarket | None:
        return self._tokens.get(mint)

    def ensure(self, mint: str) -> TokenMarket:
        return self._tokens.setdefault(mint, TokenMarket(mint))

    def apply(
        self, event: TokenCreated | TradeObserved | PoolMigrated, at: datetime
    ) -> TokenMarket:
        market = self.ensure(event.mint)
        if isinstance(event, TokenCreated):
            market.creator = event.creator
            self._update_reserves(market, event.v_sol_lamports, event.v_tokens_raw, at)
        elif isinstance(event, TradeObserved):
            market.trades.append((at, event.side, event.sol_amount_lamports or 0, event.trader))
            price = implied_price(event.sol_amount_lamports, event.token_amount_raw)
            if price is not None:
                market.implied_prices.append((at, price))
            if not market.migrated:
                self._update_reserves(market, event.v_sol_lamports, event.v_tokens_raw, at)
        elif market.migrated_at is None:
            market.migrated_at = at
            market.v_sol = market.v_tokens = None  # curve reserves no longer executable
        return market

    @staticmethod
    def _update_reserves(
        market: TokenMarket, v_sol: int | None, v_tokens: int | None, at: datetime
    ) -> None:
        if v_sol and v_tokens and v_sol > 0 and v_tokens > 0:
            market.v_sol, market.v_tokens, market.updated_at = v_sol, v_tokens, at

    def prune_idle(self, now: datetime, idle: timedelta, keep: set[str]) -> None:
        """Forget tokens without any update for ``idle`` (memory stays bounded)."""
        for mint, market in list(self._tokens.items()):
            last = market.updated_at or market.migrated_at
            if mint not in keep and (last is None or now - last > idle):
                if market.trades and now - market.trades[-1][0] <= idle:
                    continue  # still trading (e.g. after migration)
                del self._tokens[mint]

    def __len__(self) -> int:
        return len(self._tokens)
