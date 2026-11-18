"""Which tokens get their trades streamed.

Every new token is followed for a fixed window (needed to label outcomes at 5/15/30/60 min).
Tokens that stay inactive are dropped early, and graduated tokens are followed again for
strategy B (post-migration). The registry is pure logic: the caller applies the returned
subscribe/unsubscribe decisions to the data provider.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True)
class TrackedToken:
    mint: str
    started_at: datetime
    window: timedelta
    trades: int = 0
    migrated: bool = False


class TrackingRegistry:
    def __init__(
        self,
        window: timedelta,
        migrated_window: timedelta,
        min_trades: int,
        min_trades_window: timedelta,
        max_tracked: int,
    ) -> None:
        self._window = window
        self._migrated_window = migrated_window
        self._min_trades = min_trades
        self._min_trades_window = min_trades_window
        self._max_tracked = max_tracked
        self._tokens: dict[str, TrackedToken] = {}
        self.rejected_capacity = 0

    def __contains__(self, mint: str) -> bool:
        return mint in self._tokens

    def __len__(self) -> int:
        return len(self._tokens)

    def mints(self) -> list[str]:
        return list(self._tokens)

    def on_created(self, mint: str, now: datetime) -> bool:
        """Start tracking a new token. Returns True if a subscription is needed."""
        if mint in self._tokens:
            return False
        if len(self._tokens) >= self._max_tracked:
            self.rejected_capacity += 1
            return False
        self._tokens[mint] = TrackedToken(mint, now, self._window)
        return True

    def on_trade(self, mint: str) -> None:
        token = self._tokens.get(mint)
        if token is not None:
            token.trades += 1

    def on_migrated(self, mint: str, now: datetime) -> bool:
        """Follow a graduated token for the post-migration window. True if newly subscribed."""
        token = self._tokens.get(mint)
        if token is not None:
            token.started_at, token.window, token.migrated = now, self._migrated_window, True
            return False
        if len(self._tokens) >= self._max_tracked:
            self.rejected_capacity += 1
            return False
        self._tokens[mint] = TrackedToken(mint, now, self._migrated_window, migrated=True)
        return True

    def expire(self, now: datetime, keep: frozenset[str] = frozenset()) -> list[str]:
        """Drop tokens whose window ended or that stayed inactive; returns mints to unsubscribe.

        Mints in ``keep`` (e.g. open paper positions) are never dropped.
        """
        expired = [m for m, t in self._tokens.items() if m not in keep and self._is_expired(t, now)]
        for mint in expired:
            del self._tokens[mint]
        return expired

    def _is_expired(self, token: TrackedToken, now: datetime) -> bool:
        age = now - token.started_at
        if age >= token.window:
            return True
        inactive = not token.migrated and token.trades < self._min_trades
        return inactive and age >= self._min_trades_window
