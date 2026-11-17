import json

import httpx
import pytest

from backend.database.enums import Severity
from backend.monitoring.alerts import TELEGRAM_MAX_LENGTH, LogOnlyAlerter, TelegramAlerter
from backend.monitoring.logging import configure_logging

TOKEN = "123456:SECRET-BOT-TOKEN"


def alerter(handler: httpx.MockTransport) -> TelegramAlerter:
    return TelegramAlerter(httpx.AsyncClient(transport=handler), TOKEN, chat_id="42")


async def test_telegram_sends_expected_payload() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    assert await alerter(httpx.MockTransport(handle)).send(Severity.ERROR, "Flux coupé", "45 s")
    body = json.loads(seen[0].content)
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == "42"
    assert body["text"] == "[ERROR] Flux coupé\n\n45 s"


async def test_telegram_http_error_returns_false() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    assert not await alerter(transport).send(Severity.INFO, "x")


async def test_telegram_transport_error_never_raises_nor_leaks_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "json")  # deliberately without registered secrets

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    assert not await alerter(httpx.MockTransport(handle)).send(Severity.CRITICAL, "x")
    output = capsys.readouterr().out
    assert "alert_delivery_failed" in output
    assert TOKEN not in output


def test_message_is_truncated_to_telegram_limit() -> None:
    text = TelegramAlerter.format_message(Severity.INFO, "t", "x" * 10_000)
    assert len(text) == TELEGRAM_MAX_LENGTH


async def test_log_only_alerter_reports_not_delivered() -> None:
    assert not await LogOnlyAlerter().send(Severity.WARNING, "x")
