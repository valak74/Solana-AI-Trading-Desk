import json
from typing import Any

import pytest

from backend.monitoring.logging import REDACTED, SecretRedactor, configure_logging, get_logger


def redact(event: dict[str, Any], secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    return dict(SecretRedactor(secrets)(None, "info", event))


def test_sensitive_keys_are_redacted() -> None:
    out = redact({"event": "x", "api_key": "abc", "TELEGRAM_BOT_TOKEN": "t", "private_key": "k"})
    assert out == {
        "event": "x",
        "api_key": REDACTED,
        "TELEGRAM_BOT_TOKEN": REDACTED,
        "private_key": REDACTED,
    }


def test_known_secret_values_are_scrubbed_anywhere() -> None:
    secret = "123456:ABCDEF-very-secret"
    out = redact(
        {
            "event": f"POST https://api.telegram.org/bot{secret}/sendMessage failed",
            "nested": {"url": f"x{secret}y", "items": [secret, "ok"]},
        },
        (secret,),
    )
    dumped = json.dumps(out)
    assert secret not in dumped
    assert out["nested"]["items"] == [REDACTED, "ok"]


def test_nested_sensitive_keys_are_redacted() -> None:
    out = redact({"event": "x", "config": {"password": "p", "host": "db"}})
    assert out["config"] == {"password": REDACTED, "host": "db"}


def test_short_values_are_not_used_as_secrets() -> None:
    # A 1-3 character "secret" would destroy unrelated log text.
    assert redact({"event": "abc"}, ("a",)) == {"event": "abc"}


def test_rendered_log_line_contains_no_secret(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "postgresql+asyncpg://desk:hunter22@db/desk"
    configure_logging("INFO", "json", [secret])
    get_logger("test").info("db_connect_failed", detail=f"cannot reach {secret}")
    line = capsys.readouterr().out.strip()
    assert "hunter22" not in line
    assert json.loads(line)["event"] == "db_connect_failed"


def test_exception_tracebacks_are_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "tok-ABCDEFGH-123"
    configure_logging("INFO", "json", [secret])
    try:
        raise RuntimeError(f"request to /bot{secret} failed")
    except RuntimeError:
        get_logger("test").exception("boom")
    assert secret not in capsys.readouterr().out
