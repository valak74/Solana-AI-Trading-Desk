"""On-chain cross-check of a sample of provider events.

Purpose: measure whether the third-party feed can be trusted (transaction exists, succeeded,
involves the announced mint) and how late we see events (receive time minus block time).
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.models import VerificationSample
from backend.database.session import transaction
from backend.integrations.helius import RpcError
from backend.monitoring.logging import get_logger
from backend.scanner.processor import VerificationRequest

log = get_logger(__name__)


class TransactionFetcher(Protocol):
    async def get_transaction(self, signature: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    found: bool
    tx_succeeded: bool | None = None
    mint_present: bool | None = None
    slot: int | None = None
    block_time: datetime | None = None
    detection_latency_ms: int | None = None
    error: str | None = None


def _transaction_accounts(tx: dict[str, Any]) -> set[str]:
    accounts: set[str] = set()
    message = (tx.get("transaction") or {}).get("message") or {}
    for key in message.get("accountKeys") or []:
        accounts.add(key if isinstance(key, str) else str((key or {}).get("pubkey")))
    meta = tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    for group in ("writable", "readonly"):
        accounts.update(a for a in loaded.get(group) or [] if isinstance(a, str))
    for balances in ("preTokenBalances", "postTokenBalances"):
        accounts.update(
            b["mint"] for b in meta.get(balances) or [] if isinstance(b, dict) and "mint" in b
        )
    return accounts


def evaluate_transaction(
    tx: dict[str, Any] | None, mint: str, received_at: datetime
) -> CheckResult:
    if tx is None:
        return CheckResult(found=False)
    meta = tx.get("meta") or {}
    block_time_raw = tx.get("blockTime")
    block_time = (
        datetime.fromtimestamp(block_time_raw, UTC) if isinstance(block_time_raw, int) else None
    )
    latency = (
        int((received_at - block_time).total_seconds() * 1000) if block_time is not None else None
    )
    slot = tx.get("slot")
    return CheckResult(
        found=True,
        tx_succeeded=meta.get("err") is None,
        mint_present=mint in _transaction_accounts(tx),
        slot=slot if isinstance(slot, int) else None,
        block_time=block_time,
        detection_latency_ms=latency,
    )


class SampleVerifier:
    def __init__(
        self,
        fetcher: TransactionFetcher,
        factory: async_sessionmaker[AsyncSession],
        *,
        max_rps: float = 2.0,
        first_delay: float = 5.0,
        retry_delay: float = 5.0,
        attempts: int = 3,
        queue_size: int = 1000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._factory = factory
        self._interval = 1.0 / max_rps
        self._first_delay = first_delay
        self._retry_delay = retry_delay
        self._attempts = attempts
        self._clock = clock
        self.queue: asyncio.Queue[VerificationRequest] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0

    def submit(self, request: VerificationRequest) -> None:
        try:
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            self.dropped += 1

    async def check(self, request: VerificationRequest) -> CheckResult:
        result = CheckResult(found=False)
        for attempt in range(self._attempts):
            await asyncio.sleep(self._first_delay if attempt == 0 else self._retry_delay)
            try:
                tx = await self._fetcher.get_transaction(request.signature)
            except (RpcError, httpx.HTTPError) as exc:
                result = CheckResult(found=False, error=type(exc).__name__)
                continue
            result = evaluate_transaction(tx, request.mint, request.received_at)
            if result.found:
                return result
        return result

    async def _store(self, request: VerificationRequest, result: CheckResult) -> None:
        async with transaction(self._factory) as session:
            session.add(
                VerificationSample(
                    source=request.source,
                    event_type=request.event_type,
                    signature=request.signature,
                    mint=request.mint,
                    received_at=request.received_at,
                    checked_at=self._clock(),
                    found=result.found,
                    tx_succeeded=result.tx_succeeded,
                    mint_present=result.mint_present,
                    slot=result.slot,
                    block_time=result.block_time,
                    detection_latency_ms=result.detection_latency_ms,
                    error=result.error,
                )
            )

    async def _process(self, request: VerificationRequest) -> None:
        result = await self.check(request)
        try:
            await self._store(request, result)
        except Exception as exc:
            log.warning("verification_not_stored", error_type=type(exc).__name__)

    async def run(self) -> None:
        """Consume requests forever, starting at most ``max_rps`` checks per second."""
        pending: set[asyncio.Task[None]] = set()
        while True:
            request = await self.queue.get()
            task = asyncio.create_task(self._process(request))
            pending.add(task)
            task.add_done_callback(pending.discard)
            await asyncio.sleep(self._interval)
