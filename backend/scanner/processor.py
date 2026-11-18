"""Turns provider items into rows to persist and tracking decisions. Pure logic, no I/O."""

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from backend.database.enums import TradeSide
from backend.scanner.dedup import RecentKeys, dedup_key
from backend.scanner.feed_monitor import FeedMonitor, GapEvent
from backend.scanner.messages import (
    PUMP_TOKEN_DECIMALS,
    MessageError,
    PoolMigrated,
    TokenCreated,
    TradeObserved,
    event_type_of,
    parse_message,
    price_sol_per_token,
)
from backend.scanner.providers.base import ConnectionStatus, ProviderItem, RawMessage
from backend.scanner.pumpfun import (
    PUMP_FUN_PROGRAM_ID,
    estimated_curve_progress,
    estimated_real_sol_lamports,
)
from backend.scanner.tracking import TrackingRegistry


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    source: str
    event_type: str
    signature: str
    mint: str
    received_at: datetime


@dataclass(slots=True)
class WriteBatch:
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    pools: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    graduated: set[str] = field(default_factory=set)
    gaps: list[tuple[str, GapEvent]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.raw_events) + len(self.snapshots) + len(self.gaps)

    def merge(self, newer: "WriteBatch") -> None:
        """Append a newer batch (used to retry after a failed write)."""
        self.raw_events.extend(newer.raw_events)
        for mint, row in newer.tokens.items():
            self.tokens.setdefault(mint, row)
        for address, row in newer.pools.items():
            self.pools.setdefault(address, row)
        self.snapshots.extend(newer.snapshots)
        self.graduated |= newer.graduated
        self.gaps.extend(newer.gaps)


@dataclass(slots=True)
class HandleResult:
    track: list[str] = field(default_factory=list)
    verify: VerificationRequest | None = None


@dataclass(slots=True)
class IngestStats:
    messages: int = 0
    bytes: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    duplicates: int = 0
    parse_errors: int = 0
    snapshots: int = 0
    disconnects: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "bytes": self.bytes,
            "by_type": dict(self.by_type),
            "duplicates": self.duplicates,
            "parse_errors": self.parse_errors,
            "snapshots": self.snapshots,
            "disconnects": self.disconnects,
        }


@dataclass(slots=True)
class _MintState:
    buys: int = 0
    sells: int = 0
    last_snapshot_at: datetime | None = None


class IngestProcessor:
    def __init__(
        self,
        registry: TrackingRegistry,
        monitor: FeedMonitor,
        *,
        tracked_pools: tuple[str, ...] = ("pump",),
        snapshot_min_interval: timedelta = timedelta(seconds=5),
        verify_sample_rate: float = 0.0,
        recent_keys: RecentKeys | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.registry = registry
        self.monitor = monitor
        self._tracked_pools = tracked_pools
        self._snapshot_interval = snapshot_min_interval
        self._verify_rate = verify_sample_rate
        self._recent = recent_keys or RecentKeys()
        self._rng = rng or random.Random()  # noqa: S311 - sampling, not cryptography
        self._mints: dict[str, _MintState] = {}
        self.batch = WriteBatch()
        self.stats = IngestStats()

    def take_batch(self) -> WriteBatch:
        batch, self.batch = self.batch, WriteBatch()
        return batch

    def take_stats(self) -> IngestStats:
        stats, self.stats = self.stats, IngestStats()
        return stats

    # --- item handling -------------------------------------------------------------------

    def handle(self, item: ProviderItem) -> HandleResult:
        if isinstance(item, ConnectionStatus):
            self.monitor.on_connection(item.connected, item.at, item.reason)
            if not item.connected:
                self.stats.disconnects += 1
            self._collect_gaps(item.source)
            return HandleResult()
        return self._handle_raw(item)

    def _handle_raw(self, msg: RawMessage) -> HandleResult:
        self.stats.messages += 1
        self.stats.bytes += msg.size_bytes
        self.monitor.on_message(msg.received_at)
        self._collect_gaps(msg.source)
        try:
            event = parse_message(msg.payload)
            event_type = event_type_of(event)
        except MessageError:
            event, event_type = None, "malformed"
            self.stats.parse_errors += 1
        self.stats.by_type[event_type] += 1

        key = dedup_key(msg.source, event_type, msg.payload)
        if self._recent.seen(key):
            self.stats.duplicates += 1
            return HandleResult()
        self.batch.raw_events.append(self._raw_row(msg, event_type, key))

        if isinstance(event, TokenCreated):
            return self._on_created(event, msg)
        if isinstance(event, TradeObserved):
            return self._on_trade(event, msg)
        if isinstance(event, PoolMigrated):
            return self._on_migrated(event, msg)
        return HandleResult()

    @staticmethod
    def _raw_row(msg: RawMessage, event_type: str, key: str) -> dict[str, Any]:
        signature = msg.payload.get("signature")
        return {
            "source": msg.source,
            "event_type": event_type,
            "dedup_key": key,
            "signature": signature if isinstance(signature, str) and len(signature) <= 88 else None,
            "received_at": msg.received_at,
            "payload": msg.payload,
        }

    def _on_created(self, event: TokenCreated, msg: RawMessage) -> HandleResult:
        is_pump = event.pool == "pump"
        self.batch.tokens[event.mint] = {
            "mint_address": event.mint,
            "creator_wallet": event.creator,
            "name": event.name,
            "symbol": event.symbol,
            "metadata_uri": event.uri,
            "launchpad": event.pool,
            "decimals": PUMP_TOKEN_DECIMALS if is_pump else None,
        }
        if event.bonding_curve and is_pump:
            self.batch.pools[event.bonding_curve] = {
                "mint": event.mint,
                "address": event.bonding_curve,
                "program_id": PUMP_FUN_PROGRAM_ID,
                "kind": "bonding_curve",
            }
        result = HandleResult(verify=self._maybe_verify(msg, "token_created", event))
        if event.pool in self._tracked_pools and self.registry.on_created(
            event.mint, msg.received_at
        ):
            result.track.append(event.mint)
        self._snapshot(
            event.mint, msg.received_at, event.v_sol_lamports, event.v_tokens_raw, force=True
        )
        return result

    def _on_trade(self, event: TradeObserved, msg: RawMessage) -> HandleResult:
        self.batch.tokens.setdefault(event.mint, {"mint_address": event.mint})
        state = self._mints.setdefault(event.mint, _MintState())
        if event.side is TradeSide.BUY:
            state.buys += 1
        else:
            state.sells += 1
        self.registry.on_trade(event.mint)
        self._snapshot(event.mint, msg.received_at, event.v_sol_lamports, event.v_tokens_raw)
        return HandleResult(verify=self._maybe_verify(msg, "trade", event))

    def _on_migrated(self, event: PoolMigrated, msg: RawMessage) -> HandleResult:
        self.batch.tokens.setdefault(event.mint, {"mint_address": event.mint})
        self.batch.graduated.add(event.mint)
        result = HandleResult()
        if self.registry.on_migrated(event.mint, msg.received_at):
            result.track.append(event.mint)
        return result

    def _maybe_verify(
        self, msg: RawMessage, event_type: str, event: TokenCreated | TradeObserved
    ) -> VerificationRequest | None:
        if self._verify_rate <= 0 or self._rng.random() >= self._verify_rate:
            return None
        return VerificationRequest(
            msg.source, event_type, event.signature, event.mint, msg.received_at
        )

    def _snapshot(
        self,
        mint: str,
        now: datetime,
        v_sol: int | None,
        v_tokens: int | None,
        *,
        force: bool = False,
    ) -> None:
        state = self._mints.setdefault(mint, _MintState())
        last = state.last_snapshot_at
        if not force and last is not None and now - last < self._snapshot_interval:
            return
        price: Decimal | None = price_sol_per_token(v_sol, v_tokens)
        if price is None and v_sol is None and v_tokens is None:
            return
        state.last_snapshot_at = now
        self.stats.snapshots += 1
        self.batch.snapshots.append(
            {
                "mint": mint,
                "timestamp": now,
                "price_sol": price,
                "liquidity_lamports": estimated_real_sol_lamports(v_sol),
                "virtual_sol_reserves": v_sol,
                "virtual_token_reserves": v_tokens,
                "curve_progress": estimated_curve_progress(v_tokens),
                "buy_count": state.buys,
                "sell_count": state.sells,
            }
        )

    # --- periodic maintenance ------------------------------------------------------------

    def tick(self, now: datetime, source: str) -> list[str]:
        """Expire tracked tokens and check feed staleness. Returns mints to unsubscribe."""
        self.monitor.check(now)
        self._collect_gaps(source)
        expired = self.registry.expire(now)
        for mint in [m for m in self._mints if m not in self.registry]:
            del self._mints[mint]  # bounded memory: only tracked tokens keep counters
        return expired

    def _collect_gaps(self, source: str) -> None:
        for gap in self.monitor.drain():
            self.batch.gaps.append((source, gap))
