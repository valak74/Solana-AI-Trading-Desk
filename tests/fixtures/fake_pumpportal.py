"""Local fake of the PumpPortal data WebSocket, for tests without internet access."""

import asyncio
import json
from typing import Any

from websockets.asyncio.server import Server, ServerConnection


class FakePumpPortal:
    """Records subscription messages; sends scripted messages; can drop connections."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.connections = 0
        self.paths: list[str] = []
        self.to_send: asyncio.Queue[str | None] = asyncio.Queue()
        self.server: Server | None = None

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.paths.append(ws.request.path if ws.request else "")

        async def reader() -> None:
            async for message in ws:
                self.received.append(json.loads(message))

        read_task = asyncio.create_task(reader())  # ends when the connection closes
        try:
            while not read_task.done():
                get = asyncio.create_task(self.to_send.get())
                await asyncio.wait({get, read_task}, return_when=asyncio.FIRST_COMPLETED)
                if not get.done():
                    get.cancel()
                    return
                text = get.result()
                if text is None:  # scripted disconnect
                    await ws.close()
                    return
                await ws.send(text)
        finally:
            read_task.cancel()

    @property
    def url(self) -> str:
        assert self.server is not None
        host, port = next(iter(self.server.sockets)).getsockname()[:2]
        return f"ws://{host}:{port}"
