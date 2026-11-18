"""Jupiter Swap API V2 quote client (quote only: no taker, no transaction, no signature).

From the documentation available at the time of writing: base URL ``https://api.jup.ag/swap/v2``,
API key in the ``x-api-key`` header, ``GET /order`` with ``inputMint``, ``outputMint`` and
``amount``; Jupiter's fee is already deducted from the returned ``outAmount``. The full schema
could not be checked from the development environment, so parsing is tolerant and any doubt
yields "no quote" (never a guessed price). Validate once with ``backend.scanner.probe``.
"""

import asyncio
import time
from dataclasses import dataclass

import httpx

from backend.monitoring.logging import get_logger

log = get_logger(__name__)

JUPITER_BASE = "https://api.jup.ag/swap/v2"
WSOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class JupiterQuote:
    in_amount: int
    out_amount: int
    fee_bps: int | None
    price_impact: str | None


class JupiterClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        base_url: str = JUPITER_BASE,
        min_interval_seconds: float = 1.0,
    ) -> None:
        self._client = client
        self._headers = {"x-api-key": api_key}
        self._base = base_url.rstrip("/")
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self.calls = 0

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def quote(self, input_mint: str, output_mint: str, amount: int) -> JupiterQuote | None:
        if amount <= 0:
            return None
        await self._throttle()
        self.calls += 1
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount)}
        try:
            response = await self._client.get(
                f"{self._base}/order", params=params, headers=self._headers
            )
        except httpx.HTTPError as exc:
            log.info("jupiter_unavailable", error_type=type(exc).__name__)
            return None
        if response.status_code != httpx.codes.OK:
            log.info("jupiter_no_quote", status=response.status_code)
            return None
        try:
            data = response.json()
            out_amount = int(data["outAmount"])
            in_amount = int(data.get("inAmount", amount))
        except (ValueError, KeyError, TypeError):
            return None
        if out_amount <= 0 or data.get("errorCode") or data.get("error"):
            return None
        fee = data.get("feeBps")
        impact = data.get("priceImpactPct", data.get("priceImpact"))
        return JupiterQuote(
            in_amount=in_amount,
            out_amount=out_amount,
            fee_bps=fee if isinstance(fee, int) else None,
            price_impact=None if impact is None else str(impact),
        )

    async def buy_quote(self, mint: str, lamports: int) -> JupiterQuote | None:
        return await self.quote(WSOL_MINT, mint, lamports)

    async def sell_quote(self, mint: str, raw_tokens: int) -> JupiterQuote | None:
        return await self.quote(mint, WSOL_MINT, raw_tokens)
