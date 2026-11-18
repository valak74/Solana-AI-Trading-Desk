"""Minimal Helius (Solana JSON-RPC) client.

The API key travels in the URL query string, so request URLs and transport error messages are
never logged; failures are reported by exception type only.
"""

from typing import Any
from urllib.parse import urlencode

import httpx


class RpcError(RuntimeError):
    """The RPC endpoint answered, but with an error (HTTP status or JSON-RPC error object)."""


class HeliusRpc:
    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/?{urlencode({'api-key': api_key})}"
        self._next_id = 0

    async def call(self, method: str, params: list[Any]) -> Any:
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

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        """Confirmed transaction (JSON encoding, v0 supported), or None if not (yet) found."""
        result = await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        )
        return result if isinstance(result, dict) else None
