"""Replays previously recorded raw messages (tests, backtests, re-processing)."""

from collections.abc import AsyncIterator, Iterable, Sequence

from backend.scanner.providers.base import ProviderItem


class ReplayProvider:
    name = "replay"

    def __init__(self, items: Iterable[ProviderItem]) -> None:
        self._items = list(items)
        self.tracked: set[str] = set()
        self.track_calls: list[list[str]] = []
        self.untrack_calls: list[list[str]] = []

    async def items(self) -> AsyncIterator[ProviderItem]:
        for item in self._items:
            yield item

    async def track(self, mints: Sequence[str]) -> None:
        self.track_calls.append(list(mints))
        self.tracked.update(mints)

    async def untrack(self, mints: Sequence[str]) -> None:
        self.untrack_calls.append(list(mints))
        self.tracked.difference_update(mints)

    async def close(self) -> None:
        return None
