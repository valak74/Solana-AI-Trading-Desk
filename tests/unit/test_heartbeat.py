import httpx

from backend.monitoring.heartbeat import Heartbeat

PING = "https://hc-ping.example/uuid"


def heartbeat(ready: bool, transport: httpx.MockTransport) -> Heartbeat:
    async def is_ready() -> bool:
        return ready

    return Heartbeat(httpx.AsyncClient(transport=transport), PING, is_ready)


async def test_pings_when_ready() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    assert await heartbeat(True, httpx.MockTransport(handle)).beat_once()
    assert calls == [PING]


async def test_does_not_ping_when_not_ready() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    assert not await heartbeat(False, httpx.MockTransport(handle)).beat_once()
    assert calls == []


async def test_readiness_exception_counts_as_not_ready() -> None:
    async def broken() -> bool:
        raise RuntimeError("db down")

    hb = Heartbeat(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        PING,
        broken,
    )
    assert not await hb.beat_once()


async def test_network_error_is_swallowed() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    assert not await heartbeat(True, httpx.MockTransport(handle)).beat_once()
