from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from backend.config import ExecutionMode, Settings

DB_URL = "postgresql+asyncpg://user:s3cretpw@db:5432/desk"


def make(**overrides: Any) -> Settings:
    return Settings(_env_file=None, database_url=SecretStr(DB_URL), **overrides)


def test_defaults_to_paper_mode() -> None:
    assert make().execution_mode is ExecutionMode.PAPER


def test_live_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="live execution is not implemented"):
        make(execution_mode="live")


def test_live_mode_rejected_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_MODE", "live")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_database_url_must_be_async_postgres() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(_env_file=None, database_url=SecretStr("sqlite:///x.db"))


def test_telegram_requires_token_and_chat_id_together() -> None:
    with pytest.raises(ValidationError, match="set together"):
        make(telegram_bot_token=SecretStr("123456:ABCDEF"))
    settings = make(telegram_bot_token=SecretStr("123456:ABCDEF"), telegram_chat_id="42")
    assert settings.telegram_chat_id == "42"


def test_secrets_are_hidden_from_repr() -> None:
    settings = make(
        telegram_bot_token=SecretStr("123456:ABCDEF"),
        telegram_chat_id="42",
        healthchecks_ping_url=SecretStr("https://hc-ping.com/uuid-secret"),
    )
    text = repr(settings) + str(settings.model_dump())
    assert "s3cretpw" not in text
    assert "123456:ABCDEF" not in text
    assert "uuid-secret" not in text


def test_secret_values_lists_only_configured_secrets() -> None:
    assert make().secret_values() == [DB_URL]
    settings = make(telegram_bot_token=SecretStr("123456:ABCDEF"), telegram_chat_id="42")
    assert set(settings.secret_values()) == {DB_URL, "123456:ABCDEF"}


def test_settings_are_immutable() -> None:
    settings = make()
    with pytest.raises(ValidationError):
        settings.execution_mode = ExecutionMode.LIVE  # type: ignore[misc]
