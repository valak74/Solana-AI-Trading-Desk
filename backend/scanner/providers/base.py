"""Data provider interface: strategies never depend on a specific provider."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RawMessage:
    source: str
    received_at: datetime
    payload: dict[str, Any]
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    source: str
    connected: bool
    at: datetime
    reason: str | None = None


ProviderItem = RawMessage | ConnectionStatus


class MarketDataProvider(Protocol):
    name: str

    def items(self) -> AsyncIterator[ProviderItem]:
        """Yield messages and connection changes forever, reconnecting on failure."""
        ...

    async def track(self, mints: Sequence[str]) -> None:
        """Start streaming trades for these mints (kept across reconnects)."""
        ...

    async def untrack(self, mints: Sequence[str]) -> None: ...

    async def close(self) -> None: ...
