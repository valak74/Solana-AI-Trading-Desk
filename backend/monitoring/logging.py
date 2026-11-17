"""Structured logging with secret redaction.

Two layers of protection:
1. Values whose key looks sensitive (token, secret, password, ...) are replaced.
2. Any known secret value (registered from settings) is scrubbed from every string,
   so a secret embedded in a URL or an exception message is still hidden.
"""

import logging
import re
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any

import structlog

REDACTED = "***"

_SENSITIVE_KEY = re.compile(
    r"(token|secret|password|passwd|passphrase|private|api[_-]?key|authorization|"
    r"ping_url|database_url|dsn|seed|mnemonic)",
    re.IGNORECASE,
)


class SecretRedactor:
    """structlog processor removing secrets from event dictionaries."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        # Longest first so a secret containing another one is fully removed.
        self._values = sorted({v for v in secret_values if len(v) >= 4}, key=len, reverse=True)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._values:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {k: self._redact_item(str(k), v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return type(value)(self._scrub(v) for v in value)
        return value

    def _redact_item(self, key: str, value: Any) -> Any:
        if _SENSITIVE_KEY.search(key) and value is not None:
            return REDACTED
        return self._scrub(value)

    def __call__(
        self, _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        for key in list(event_dict):
            event_dict[key] = self._redact_item(key, event_dict[key])
        return event_dict


def _stdout_logger_factory(*_args: Any) -> structlog.PrintLogger:
    # Resolve sys.stdout at call time (not import time) so redirected streams are honoured.
    return structlog.PrintLogger(file=sys.stdout)


def configure_logging(
    level: str = "INFO", fmt: str = "json", secret_values: Iterable[str] = ()
) -> None:
    redactor = SecretRedactor(secret_values)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redactor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=_stdout_logger_factory,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
