"""Helius (Solana JSON-RPC) client with a daily credit budget.

The API key travels in the URL query string, so request URLs and transport error messages are
never logged; failures are reported by exception type only.

Every call is charged against a ``CreditBudget`` before being sent. Helius documents
``getTransaction`` and ``getSignaturesForAddress`` at 1 credit; other costs are configurable
estimates (check the Helius dashboard and adjust ``method_costs``).
"""

from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

import httpx


class RpcError(RuntimeError):
    """The RPC endpoint answered, but with an error (HTTP status or JSON-RPC error object)."""


class BudgetExceeded(RuntimeError):
    """The daily credit budget would be exceeded: the call was not sent."""


DEFAULT_METHOD_COSTS: dict[str, int] = {
    "getTransaction": 1,
    "getSignaturesForAddress": 1,
    "getAccountInfo": 1,
    "getMultipleAccounts": 1,
    "getTokenLargestAccounts": 1,
}


class CreditBudget:
    """Daily (UTC) credit allowance shared by every component that calls the RPC."""

    def __init__(
        self,
        daily_limit: int,
        method_costs: dict[str, int] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.daily_limit = daily_limit
        self._costs = DEFAULT_METHOD_COSTS | (method_costs or {})
        self._clock = clock
        self._day: date = clock().date()
        self.spent = 0
        self.calls: Counter[str] = Counter()

    def cost(self, method: str) -> int:
        return self._costs.get(method, 1)

    def _roll(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day, self.spent = today, 0

    def remaining(self) -> int:
        self._roll()
        return max(0, self.daily_limit - self.spent)

    def charge(self, method: str) -> None:
        self._roll()
        cost = self.cost(method)
        if self.spent + cost > self.daily_limit:
            raise BudgetExceeded(method)
        self.spent += cost
        self.calls[method] += 1


class HeliusRpc:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        budget: CreditBudget | None = None,
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/?{urlencode({'api-key': api_key})}"
        self._next_id = 0
        self.budget = budget

    async def call(self, method: str, params: list[Any]) -> Any:
        if self.budget is not None:
            self.budget.charge(method)
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        response = await self._client.post(self._url, json=body)
        if response.status_code != httpx.codes.OK:
            raise RpcError(f"{method}: HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise RpcError(f"{method}: malformed response")
        if data.get("error") is not None:
            error = data["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise RpcError(f"{method}: RPC error {code}")
        return data.get("result")

    async def get_transaction(
        self, signature: str, encoding: str = "json"
    ) -> dict[str, Any] | None:
        """Confirmed transaction (v0 supported), or None if not (yet) found."""
        result = await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": encoding,
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        )
        return result if isinstance(result, dict) else None

    async def get_account_info(
        self, address: str, encoding: str = "jsonParsed"
    ) -> dict[str, Any] | None:
        result = await self.call(
            "getAccountInfo", [address, {"encoding": encoding, "commitment": "confirmed"}]
        )
        value = result.get("value") if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

    async def get_multiple_accounts(
        self, addresses: list[str], encoding: str = "jsonParsed"
    ) -> list[dict[str, Any] | None]:
        result = await self.call(
            "getMultipleAccounts", [addresses, {"encoding": encoding, "commitment": "confirmed"}]
        )
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != len(addresses):
            raise RpcError("getMultipleAccounts: unexpected response shape")
        return [v if isinstance(v, dict) else None for v in values]

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list):
            raise RpcError("getTokenLargestAccounts: unexpected response shape")
        return [v for v in values if isinstance(v, dict)]

    async def get_signatures_for_address(
        self, address: str, limit: int = 1000, before: str | None = None
    ) -> list[dict[str, Any]]:
        """Signatures touching ``address``, newest first."""
        options: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if before is not None:
            options["before"] = before
        result = await self.call("getSignaturesForAddress", [address, options])
        if not isinstance(result, list):
            raise RpcError("getSignaturesForAddress: unexpected response shape")
        return [s for s in result if isinstance(s, dict)]
