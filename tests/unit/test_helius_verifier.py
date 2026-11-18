import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from backend.integrations.helius import HeliusRpc, RpcError
from backend.scanner.processor import VerificationRequest
from backend.scanner.verifier import SampleVerifier, evaluate_transaction
from tests.fixtures.pumpportal import mint, sig

KEY = "helius-secret-key"
BLOCK = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def rpc(handler: Any) -> HeliusRpc:
    return HeliusRpc(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), "https://rpc.example", KEY
    )


def tx(mint_address: str, *, err: object = None, in_balances: bool = True) -> dict[str, Any]:
    meta: dict[str, Any] = {"err": err, "loadedAddresses": {"writable": [], "readonly": []}}
    if in_balances:
        meta["postTokenBalances"] = [{"mint": mint_address, "uiTokenAmount": {}}]
    return {
        "slot": 300_000_000,
        "blockTime": int(BLOCK.timestamp()),
        "meta": meta,
        "transaction": {"message": {"accountKeys": ["11111111111111111111111111111111"]}},
    }


async def test_get_transaction_request_and_result() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": tx(mint(1))})

    result = await rpc(handle).get_transaction(sig(1))
    assert result is not None and result["slot"] == 300_000_000
    body = json.loads(seen[0].content)
    assert body["method"] == "getTransaction"
    assert body["params"][1]["maxSupportedTransactionVersion"] == 0
    assert seen[0].url.params["api-key"] == KEY


async def test_not_found_returns_none() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})

    assert await rpc(handle).get_transaction(sig(1)) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, text="slow down"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602}}),
        httpx.Response(200, json=["unexpected"]),
    ],
)
async def test_errors_raise_without_leaking_key(response: httpx.Response) -> None:
    with pytest.raises(RpcError) as info:
        await rpc(lambda _: response).get_transaction(sig(1))
    assert KEY not in str(info.value)


def test_evaluate_transaction() -> None:
    received = BLOCK + timedelta(milliseconds=1500)
    ok = evaluate_transaction(tx(mint(1)), mint(1), received)
    assert (ok.found, ok.tx_succeeded, ok.mint_present) == (True, True, True)
    assert ok.detection_latency_ms == 1500
    assert ok.block_time == BLOCK

    bad_tx = tx(mint(1), err={"InstructionError": [0, "x"]})
    failed = evaluate_transaction(bad_tx, mint(1), received)
    assert failed.tx_succeeded is False

    other = evaluate_transaction(tx(mint(1), in_balances=False), mint(1), received)
    assert other.mint_present is False

    assert evaluate_transaction(None, mint(1), received).found is False


class FakeFetcher:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.calls = 0

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        response = self._responses[self.calls]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        result: dict[str, Any] | None = response
        return result


def request() -> VerificationRequest:
    return VerificationRequest("pumpportal", "trade", sig(1), mint(1), BLOCK)


def verifier(fetcher: FakeFetcher, queue_size: int = 10) -> SampleVerifier:
    return SampleVerifier(
        fetcher,
        None,  # type: ignore[arg-type]  # check() never touches the database
        first_delay=0,
        retry_delay=0,
        attempts=3,
        queue_size=queue_size,
    )


async def test_check_retries_until_found() -> None:
    fetcher = FakeFetcher([RpcError("x"), None, tx(mint(1))])
    result = await verifier(fetcher).check(request())
    assert result.found and fetcher.calls == 3


async def test_check_reports_last_error_when_never_found() -> None:
    fetcher = FakeFetcher([None, None, RpcError("x")])
    result = await verifier(fetcher).check(request())
    assert not result.found and result.error == "RpcError"


def test_full_queue_drops_and_counts() -> None:
    v = verifier(FakeFetcher([]), queue_size=1)
    v.submit(request())
    v.submit(request())
    assert v.dropped == 1
