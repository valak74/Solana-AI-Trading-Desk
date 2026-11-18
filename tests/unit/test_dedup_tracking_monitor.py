from datetime import UTC, datetime, timedelta

import pytest

from backend.scanner.dedup import RecentKeys, dedup_key
from backend.scanner.feed_monitor import FeedMonitor, GapClosed, GapOpened
from backend.scanner.tracking import TrackingRegistry
from tests.fixtures.pumpportal import mint, sig, trade_msg

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


# --- dedup -------------------------------------------------------------------------------


def test_identical_payload_same_key_regardless_of_key_order() -> None:
    a = {"signature": sig(1), "mint": mint(1), "x": 1}
    b = {"x": 1, "mint": mint(1), "signature": sig(1)}
    assert dedup_key("pp", "trade", a) == dedup_key("pp", "trade", b)


def test_two_trades_in_one_transaction_are_distinct() -> None:
    one = trade_msg(mint(1), sig(1), tokenAmount=10)
    two = trade_msg(mint(1), sig(1), tokenAmount=20)
    assert dedup_key("pp", "trade", one) != dedup_key("pp", "trade", two)


def test_recent_keys_evicts_oldest() -> None:
    keys = RecentKeys(capacity=2)
    assert not keys.seen("a")
    assert not keys.seen("b")
    assert keys.seen("a")  # refreshes "a"
    assert not keys.seen("c")  # evicts "b"
    assert not keys.seen("b")
    with pytest.raises(ValueError):
        RecentKeys(0)


# --- tracking ----------------------------------------------------------------------------


def registry(max_tracked: int = 10) -> TrackingRegistry:
    return TrackingRegistry(
        window=timedelta(minutes=60),
        migrated_window=timedelta(minutes=360),
        min_trades=5,
        min_trades_window=timedelta(minutes=10),
        max_tracked=max_tracked,
    )


def test_inactive_token_dropped_after_min_trades_window() -> None:
    reg = registry()
    assert reg.on_created("m1", T0)
    assert not reg.on_created("m1", T0)  # already tracked
    assert reg.expire(T0 + timedelta(minutes=9)) == []
    assert reg.expire(T0 + timedelta(minutes=10)) == ["m1"]


def test_active_token_kept_until_window_end() -> None:
    reg = registry()
    reg.on_created("m1", T0)
    for _ in range(5):
        reg.on_trade("m1")
    assert reg.expire(T0 + timedelta(minutes=59)) == []
    assert reg.expire(T0 + timedelta(minutes=60)) == ["m1"]
    assert len(reg) == 0


def test_capacity_limit_is_counted() -> None:
    reg = registry(max_tracked=1)
    assert reg.on_created("m1", T0)
    assert not reg.on_created("m2", T0)
    assert reg.rejected_capacity == 1


def test_migration_extends_or_starts_tracking() -> None:
    reg = registry()
    reg.on_created("m1", T0)
    assert not reg.on_migrated("m1", T0 + timedelta(minutes=30))  # already subscribed
    assert reg.on_migrated("m2", T0)  # new subscription
    # Migrated tokens are not dropped for inactivity, and follow the longer window.
    assert reg.expire(T0 + timedelta(minutes=120)) == []
    assert sorted(reg.expire(T0 + timedelta(minutes=391))) == ["m1", "m2"]


# --- feed monitor ------------------------------------------------------------------------


def test_stale_feed_opens_gap_at_last_message_and_closes_on_next() -> None:
    mon = FeedMonitor(timedelta(seconds=60))
    mon.on_connection(True, T0)
    mon.on_message(T0)
    assert mon.is_fresh(T0 + timedelta(seconds=30))
    mon.check(T0 + timedelta(seconds=61))
    assert not mon.is_fresh(T0 + timedelta(seconds=61))
    mon.check(T0 + timedelta(seconds=90))  # still one gap only
    mon.on_message(T0 + timedelta(seconds=120))
    assert mon.drain() == [
        GapOpened(T0, "stale"),
        GapClosed(T0, T0 + timedelta(seconds=120), "stale"),
    ]
    assert mon.drain() == []
    assert mon.is_fresh(T0 + timedelta(seconds=121))


def test_disconnect_opens_gap_immediately() -> None:
    mon = FeedMonitor(timedelta(seconds=60))
    mon.on_connection(True, T0)
    mon.on_message(T0)
    mon.on_connection(False, T0 + timedelta(seconds=5), "ConnectionClosed")
    assert mon.in_gap
    assert not mon.is_fresh(T0 + timedelta(seconds=5))
    assert mon.drain() == [GapOpened(T0, "disconnected:ConnectionClosed")]


def test_restart_gap_covers_downtime() -> None:
    last_run = T0 - timedelta(hours=8)
    mon = FeedMonitor(timedelta(seconds=60), last_seen=last_run)
    mon.on_connection(True, T0)
    mon.on_message(T0 + timedelta(seconds=2))
    assert mon.drain() == [
        GapOpened(last_run, "service_down"),
        GapClosed(last_run, T0 + timedelta(seconds=2), "service_down"),
    ]


def test_no_data_yet_is_not_fresh_and_opens_no_gap() -> None:
    mon = FeedMonitor(timedelta(seconds=60))
    mon.on_connection(True, T0)
    mon.check(T0 + timedelta(minutes=5))
    assert not mon.is_fresh(T0 + timedelta(minutes=5))
    assert mon.drain() == []
