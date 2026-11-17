"""Operator alerts. Telegram is the only channel; without configuration alerts are just logged.

Alert delivery must never crash the caller, and must never leak the bot token: the token is
part of the Telegram URL, so transport errors are logged by type only, never by message.
"""

from typing import Protocol

import httpx

from backend.database.enums import Severity
from backend.monitoring.logging import get_logger

log = get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_LENGTH = 4096


class Alerter(Protocol):
    async def send(self, severity: Severity, title: str, body: str = "") -> bool:
        """Deliver an alert. Returns True when delivered, False otherwise. Never raises."""
        ...


class LogOnlyAlerter:
    async def send(self, severity: Severity, title: str, body: str = "") -> bool:
        log.info(
            "alert_not_delivered", reason="no_channel_configured", severity=severity, title=title
        )
        return False


class TelegramAlerter:
    def __init__(self, client: httpx.AsyncClient, bot_token: str, chat_id: str) -> None:
        self._client = client
        self._url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    @staticmethod
    def format_message(severity: Severity, title: str, body: str) -> str:
        text = f"[{severity.upper()}] {title}"
        if body:
            text = f"{text}\n\n{body}"
        return text[:TELEGRAM_MAX_LENGTH]

    async def send(self, severity: Severity, title: str, body: str = "") -> bool:
        payload = {
            "chat_id": self._chat_id,
            "text": self.format_message(severity, title, body),
            "disable_web_page_preview": True,
        }
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("alert_delivery_failed", channel="telegram", error_type=type(exc).__name__)
            return False
        if response.status_code != httpx.codes.OK:
            log.warning("alert_delivery_failed", channel="telegram", status=response.status_code)
            return False
        return True
