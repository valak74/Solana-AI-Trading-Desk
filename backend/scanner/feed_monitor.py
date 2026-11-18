"""Detection of data gaps (PC off, network loss, provider outage).

A gap starts at the last message actually received (data stopped then, whatever we noticed
later) and ends at the first message after it. Gaps are persisted and excluded from strategy
evaluation; nothing downstream may assume continuous data across a gap.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class GapOpened:
    started_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class GapClosed:
    started_at: datetime
    ended_at: datetime
    reason: str


GapEvent = GapOpened | GapClosed


class FeedMonitor:
    def __init__(self, stale_after: timedelta, last_seen: datetime | None = None) -> None:
        """``last_seen``: time of the last stored event before this process started, if any."""
        self._stale_after = stale_after
        self._last_message_at = last_seen
        self._connected = False
        self._open_gap: GapOpened | None = (
            GapOpened(last_seen, "service_down") if last_seen is not None else None
        )
        self._pending: list[GapEvent] = [self._open_gap] if self._open_gap else []

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    @property
    def in_gap(self) -> bool:
        return self._open_gap is not None

    def _open(self, started_at: datetime, reason: str) -> None:
        if self._open_gap is None:
            self._open_gap = GapOpened(started_at, reason)
            self._pending.append(self._open_gap)

    def on_message(self, now: datetime) -> None:
        if self._open_gap is not None:
            gap = self._open_gap
            self._pending.append(GapClosed(gap.started_at, now, gap.reason))
            self._open_gap = None
        self._last_message_at = now

    def on_connection(self, connected: bool, now: datetime, reason: str | None = None) -> None:
        self._connected = connected
        if not connected and self._last_message_at is not None:
            self._open(self._last_message_at, f"disconnected:{reason or 'unknown'}")

    def check(self, now: datetime) -> None:
        last = self._last_message_at
        if last is not None and now - last > self._stale_after:
            self._open(last, "stale")

    def drain(self) -> list[GapEvent]:
        events, self._pending = self._pending, []
        return events

    def is_fresh(self, now: datetime) -> bool:
        last = self._last_message_at
        return (
            self._connected
            and self._open_gap is None
            and last is not None
            and now - last <= self._stale_after
        )
