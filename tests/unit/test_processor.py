import random
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.scanner.feed_monitor import FeedMonitor, GapOpened
from backend.scanner.processor import IngestProcessor
from backend.scanner.providers.base import ConnectionStatus, RawMessage
from backend.scanner.pumpfun import PUMP_FUN_PROGRAM_ID
from backend.scanner.tracking import TrackingRegistry
from tests.fixtures.pumpportal import create_msg, migration_msg, mint, sig, trade_msg

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def processor(verify_rate: float = 0.0, max_tracked: int = 100) -> IngestProcessor:
    reg = TrackingRegistry(
        timedelta(minutes=60), timedelta(minutes=360), 5, timedelta(minutes=10), max_tracked
    )
    return IngestProcessor(
        reg,
        FeedMonitor(timedelta(seconds=60)),
        snapshot_min_interval=timedelta(seconds=5),
        verify_sample_rate=verify_rate,
        rng=random.Random(1),
    )


def raw(payload: dict[str, Any], at: datetime = T0) -> RawMessage:
    return RawMessage("pumpportal", at, payload, 300)


def test_token_creation_produces_rows_and_subscription() -> None:
    p = processor()
    result = p.handle(raw(create_msg(mint(1), sig(1))))
    assert result.track == [mint(1)]
    batch = p.take_batch()
    assert [r["event_type"] for r in batch.raw_events] == ["token_created"]
    assert batch.tokens[mint(1)]["symbol"] == "TEST"
    assert batch.tokens[mint(1)]["decimals"] == 6
    (pool,) = batch.pools.values()
    assert pool["program_id"] == PUMP_FUN_PROGRAM_ID
    (snap,) = batch.snapshots
    assert snap["mint"] == mint(1)
    assert snap["virtual_sol_reserves"] == 31_010_000_000
    assert snap["liquidity_lamports"] == 1_010_000_000
    assert snap["price_sol"] is not None


def test_duplicates_are_dropped_and_counted() -> None:
    p = processor()
    p.handle(raw(create_msg(mint(1), sig(1))))
    p.handle(raw(create_msg(mint(1), sig(1)), T0 + timedelta(seconds=1)))
    assert len(p.take_batch().raw_events) == 1
    assert p.stats.duplicates == 1


def test_snapshots_are_throttled_per_token() -> None:
    p = processor()
    p.handle(raw(create_msg(mint(1), sig(1))))
    p.handle(raw(trade_msg(mint(1), sig(2), tokenAmount=1), T0 + timedelta(seconds=1)))
    p.handle(raw(trade_msg(mint(1), sig(3), tokenAmount=2), T0 + timedelta(seconds=6)))
    p.handle(raw(trade_msg(mint(1), sig(4), "sell"), T0 + timedelta(seconds=7)))
    snaps = p.take_batch().snapshots
    assert [s["timestamp"] for s in snaps] == [T0, T0 + timedelta(seconds=6)]
    assert (snaps[-1]["buy_count"], snaps[-1]["sell_count"]) == (2, 0)


def test_trade_for_unknown_token_creates_minimal_row() -> None:
    p = processor()
    p.handle(raw(trade_msg(mint(9), sig(9))))
    assert p.take_batch().tokens[mint(9)] == {"mint_address": mint(9), "first_seen_at": T0}


def test_migration_marks_graduated_and_subscribes() -> None:
    p = processor()
    result = p.handle(raw(migration_msg(mint(5), sig(5))))
    assert result.track == [mint(5)]
    assert p.take_batch().graduated == {mint(5)}


def test_malformed_message_is_kept_raw() -> None:
    p = processor()
    p.handle(raw({"txType": "buy", "mint": "bad", "signature": "bad"}))
    p.handle(raw({"_unparseable": "<html>"}))
    types = [r["event_type"] for r in p.take_batch().raw_events]
    assert types == ["malformed", "unknown"]
    assert p.stats.parse_errors == 1


def test_other_launchpads_are_recorded_but_not_tracked() -> None:
    p = processor()
    result = p.handle(raw(create_msg(mint(1), sig(1), pool="bonk")))
    assert result.track == []
    batch = p.take_batch()
    assert batch.pools == {}
    assert batch.tokens[mint(1)]["decimals"] is None


def test_verification_sampling() -> None:
    assert processor(verify_rate=0.0).handle(raw(create_msg(mint(1), sig(1)))).verify is None
    request = processor(verify_rate=1.0).handle(raw(create_msg(mint(1), sig(1)))).verify
    assert request is not None
    assert (request.signature, request.event_type) == (sig(1), "token_created")


def test_disconnect_produces_gap_and_tick_expires_tokens() -> None:
    p = processor()
    p.handle(ConnectionStatus("pumpportal", True, T0))
    p.handle(raw(create_msg(mint(1), sig(1))))
    p.handle(ConnectionStatus("pumpportal", False, T0 + timedelta(seconds=3), "OSError"))
    gaps = p.take_batch().gaps
    assert gaps == [("pumpportal", GapOpened(T0, "disconnected:OSError"))]
    assert p.stats.disconnects == 1
    assert p.tick(T0 + timedelta(minutes=10), "pumpportal") == [mint(1)]  # no trades: inactive


def test_stats_are_reset_when_taken() -> None:
    p = processor()
    p.handle(raw(create_msg(mint(1), sig(1))))
    stats = p.take_stats()
    assert stats.messages == 1
    assert stats.bytes == 300
    assert stats.as_payload()["by_type"] == {"token_created": 1}
    assert p.stats.messages == 0
