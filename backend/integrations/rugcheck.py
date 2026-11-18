"""Optional second opinion from RugCheck's public API.

No official schema was available when this was written, so parsing is tolerant: only risks
explicitly labelled ``danger`` are reported, and any failure returns None (the internal engine
remains the authority; RugCheck being down never blocks an analysis).
"""

import httpx

from backend.monitoring.logging import get_logger

log = get_logger(__name__)

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"


class RugCheckClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str = RUGCHECK_BASE) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def dangers(self, mint: str) -> list[str] | None:
        try:
            response = await self._client.get(f"{self._base}/tokens/{mint}/report/summary")
        except httpx.HTTPError as exc:
            log.info("rugcheck_unavailable", error_type=type(exc).__name__)
            return None
        if response.status_code != httpx.codes.OK:
            log.info("rugcheck_unavailable", status=response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        risks = data.get("risks") if isinstance(data, dict) else None
        if not isinstance(risks, list):
            return None
        return sorted(
            str(r.get("name"))[:64]
            for r in risks
            if isinstance(r, dict) and str(r.get("level", "")).lower() == "danger"
        )
