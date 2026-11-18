"""Small async helpers for tests."""

import asyncio
from collections.abc import Awaitable, Callable


async def wait_until(condition: Callable[[], bool], within: float = 5.0) -> None:
    """Poll a condition until it holds (tests only: real code reacts to events)."""
    async with asyncio.timeout(within):
        while not condition():  # noqa: ASYNC110 - polling external state in tests
            await asyncio.sleep(0.01)


async def wait_until_async(condition: Callable[[], Awaitable[bool]], within: float = 5.0) -> None:
    """Async-condition variant of ``wait_until``."""
    async with asyncio.timeout(within):
        while not await condition():  # noqa: ASYNC110 - polling external state in tests
            await asyncio.sleep(0.05)
