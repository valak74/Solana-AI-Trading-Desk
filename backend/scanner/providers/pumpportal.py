"""PumpPortal data WebSocket client.

Rules followed:
- exactly one connection at a time (PumpPortal asks clients not to open one per token);
- all token subscriptions are sent on that connection and replayed after every reconnect;
- exponential backoff with jitter between reconnection attempts;
- the API key is part of the URL, so the URL is never logged and errors are logged by type only.
"""

import asyncio
import json
import random
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from backend.monitoring.logging import get_logger
from backend.scanner.messages import decode_json
from backend.scanner.providers.base import ConnectionStatus, ProviderItem, RawMessage

log = get_logger(__name__)

SOURCE = "pumpportal"
KEYS_PER_MESSAGE = 50
STABLE_CONNECTION_SECONDS = 60.0

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Full-jitter exponential backoff: random in [0, min(cap, base * 2**attempt)]."""
    upper = min(cap, base * (2 ** min(attempt, 16)))
    return random.uniform(0, upper)  # noqa: S311 - jitter, not cryptography


def build_url(base_url: str, api_key: str | None) -> str:
    if not api_key:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'api-key': api_key})}"


def chunked(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


class PumpPortalProvider:
    name = SOURCE

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        *,
        idle_timeout: float = 120.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        proxy: str | Literal[True] | None = True,
        clock: Clock = _utcnow,
    ) -> None:
        self._url = build_url(url, api_key)
        self._idle_timeout = idle_timeout
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._proxy = proxy
        self._clock = clock
        self._tracked: set[str] = set()
        self._ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    @property
    def tracked(self) -> frozenset[str]:
        return frozenset(self._tracked)

    async def _send(self, ws: ClientConnection, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send(json.dumps(message))

    async def _send_keys(self, ws: ClientConnection, method: str, mints: Sequence[str]) -> None:
        for keys in chunked(mints, KEYS_PER_MESSAGE):
            await self._send(ws, {"method": method, "keys": keys})

    async def _subscribe_all(self, ws: ClientConnection) -> None:
        await self._send(ws, {"method": "subscribeNewToken"})
        await self._send(ws, {"method": "subscribeMigration"})
        await self._send_keys(ws, "subscribeTokenTrade", sorted(self._tracked))

    async def track(self, mints: Sequence[str]) -> None:
        new = [m for m in mints if m not in self._tracked]
        self._tracked.update(new)
        ws = self._ws
        if ws is not None and new:
            try:
                await self._send_keys(ws, "subscribeTokenTrade", new)
            except WebSocketException as exc:  # replayed on reconnect
                log.warning("pumpportal_subscribe_failed", error_type=type(exc).__name__)

    async def untrack(self, mints: Sequence[str]) -> None:
        gone = [m for m in mints if m in self._tracked]
        self._tracked.difference_update(gone)
        ws = self._ws
        if ws is not None and gone:
            try:
                await self._send_keys(ws, "unsubscribeTokenTrade", gone)
            except WebSocketException as exc:
                log.warning("pumpportal_unsubscribe_failed", error_type=type(exc).__name__)

    async def close(self) -> None:
        self._closed = True
        ws = self._ws
        if ws is not None:
            await ws.close()

    def _raw(self, text: str | bytes) -> RawMessage:
        size = len(text)
        try:
            payload = decode_json(text)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            preview = text[:500] if isinstance(text, str) else repr(text[:500])
            payload = {"_unparseable": preview}
        return RawMessage(SOURCE, self._clock(), payload, size)

    async def _session(self) -> AsyncIterator[ProviderItem]:
        async with connect(
            self._url,
            proxy=self._proxy,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
        ) as ws:
            self._ws = ws
            try:
                await self._subscribe_all(ws)
                yield ConnectionStatus(SOURCE, True, self._clock())
                while True:
                    async with asyncio.timeout(self._idle_timeout):
                        text = await ws.recv()
                    yield self._raw(text)
            finally:
                self._ws = None

    async def items(self) -> AsyncIterator[ProviderItem]:
        attempt = 0
        while not self._closed:
            started = self._clock()
            reason = "closed"
            try:
                async for item in self._session():
                    yield item
            except (WebSocketException, OSError, TimeoutError) as exc:
                reason = type(exc).__name__
            if self._closed:
                return
            log.warning("pumpportal_disconnected", reason=reason)
            yield ConnectionStatus(SOURCE, False, self._clock(), reason)
            stable = (self._clock() - started).total_seconds() >= STABLE_CONNECTION_SECONDS
            attempt = 0 if stable else attempt + 1
            await asyncio.sleep(backoff_delay(attempt, self._backoff_base, self._backoff_cap))
