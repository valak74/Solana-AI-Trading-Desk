"""PumpPortal client against a local fake WebSocket server (no internet needed)."""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from websockets.asyncio.server import serve

from backend.monitoring.logging import configure_logging
from backend.scanner.providers.base import ConnectionStatus, ProviderItem, RawMessage
from backend.scanner.providers.pumpportal import (
    PumpPortalProvider,
    backoff_delay,
    build_url,
    chunked,
)
from tests.fixtures.fake_pumpportal import FakePumpPortal
from tests.fixtures.helpers import wait_until
from tests.fixtures.pumpportal import create_text, mint, sig


@pytest.fixture
async def fake() -> AsyncIterator[FakePumpPortal]:
    server = FakePumpPortal()
    async with serve(server.handler, "127.0.0.1", 0) as srv:
        server.server = srv
        yield server


async def next_item(items: AsyncIterator[ProviderItem]) -> ProviderItem:
    return await asyncio.wait_for(anext(items), timeout=5)


def methods(fake: FakePumpPortal) -> list[str]:
    return [m["method"] for m in fake.received]


async def test_subscribes_and_streams_decimal_payloads(fake: FakePumpPortal) -> None:
    provider = PumpPortalProvider(fake.url, "k3y-secret", proxy=None)
    items = provider.items()
    status = await next_item(items)
    assert isinstance(status, ConnectionStatus) and status.connected
    assert fake.paths == ["/?api-key=k3y-secret"]
    await wait_until(lambda: len(fake.received) >= 2)
    assert methods(fake)[:2] == ["subscribeNewToken", "subscribeMigration"]

    await fake.to_send.put(create_text(mint(1), sig(1)))
    message = await next_item(items)
    assert isinstance(message, RawMessage)
    assert message.payload["mint"] == mint(1)
    assert message.payload["solAmount"] == Decimal("1.000000001")
    assert message.size_bytes > 0
    await provider.close()


async def test_track_while_connected_and_replay_after_reconnect(fake: FakePumpPortal) -> None:
    provider = PumpPortalProvider(fake.url, None, proxy=None, backoff_base=0.01, backoff_cap=0.05)
    items = provider.items()
    await next_item(items)
    await provider.track([mint(1), mint(2)])
    await provider.track([mint(1)])  # already tracked: no new message
    await wait_until(lambda: "subscribeTokenTrade" in methods(fake))
    assert fake.received[-1] == {"method": "subscribeTokenTrade", "keys": [mint(1), mint(2)]}

    await provider.untrack([mint(2)])
    await wait_until(lambda: "unsubscribeTokenTrade" in methods(fake))

    await fake.to_send.put(None)  # server drops the connection
    lost = await next_item(items)
    assert isinstance(lost, ConnectionStatus) and not lost.connected
    back = await next_item(items)
    assert isinstance(back, ConnectionStatus) and back.connected
    assert fake.connections == 2
    await wait_until(lambda: methods(fake).count("subscribeTokenTrade") == 2)
    assert fake.received[-1] == {"method": "subscribeTokenTrade", "keys": [mint(1)]}
    await provider.close()


async def test_unparseable_message_is_wrapped(fake: FakePumpPortal) -> None:
    provider = PumpPortalProvider(fake.url, proxy=None)
    items = provider.items()
    await next_item(items)
    await fake.to_send.put("<html>oops</html>")
    message = await next_item(items)
    assert isinstance(message, RawMessage)
    assert message.payload == {"_unparseable": "<html>oops</html>"}
    await provider.close()


async def test_connection_failure_never_logs_api_key(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")  # no secrets registered: the client itself must not leak
    provider = PumpPortalProvider("ws://127.0.0.1:1", "SUPER-SECRET-KEY", proxy=None)
    items = provider.items()
    status = await next_item(items)
    assert isinstance(status, ConnectionStatus) and not status.connected
    await provider.close()
    assert "SUPER-SECRET-KEY" not in capsys.readouterr().out


def test_url_and_helpers() -> None:
    assert build_url("wss://x/api/data", None) == "wss://x/api/data"
    assert build_url("wss://x/api/data", "a b&c") == "wss://x/api/data?api-key=a+b%26c"
    assert build_url("wss://x/api/data?v=1", "k") == "wss://x/api/data?v=1&api-key=k"
    assert chunked(["a", "b", "c"], 2) == [["a", "b"], ["c"]]
    for attempt in range(30):
        assert 0 <= backoff_delay(attempt, base=1.0, cap=60.0) <= 60.0
