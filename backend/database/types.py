"""Column types for financial data. No float is ever used for an accounting amount.

- Lamports (SOL base units) fit in a signed BIGINT (total SOL supply is ~6e17 lamports).
- Raw SPL token amounts are u64 and can exceed BIGINT, so they are stored as NUMERIC(20, 0)
  and exposed to Python as ``int``.
- Prices are ``Decimal`` in NUMERIC(38, 18): SOL per whole token (UI units).
"""

from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Enum, Numeric
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

PRICE = Numeric(38, 18)
SCORE = Numeric(5, 2)
RATIO = Numeric(12, 6)


class U64(TypeDecorator[int]):
    """Unsigned 64-bit token amount stored losslessly, returned as ``int``."""

    impl = Numeric(20, 0)
    cache_ok = True

    def process_bind_param(self, value: int | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"U64 expects int, got {type(value).__name__}")
        if not 0 <= value < 2**64:
            raise ValueError("U64 value out of range")
        return Decimal(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> int | None:
        return None if value is None else int(value)


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Store a StrEnum as VARCHAR with a named CHECK constraint on its values."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )
