from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from backend.database.types import U64

DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def test_u64_roundtrip_is_lossless() -> None:
    value = 2**64 - 1  # beyond float and BIGINT precision
    bound = U64().process_bind_param(value, DIALECT)
    assert bound == Decimal(value)
    assert U64().process_result_value(bound, DIALECT) == value


@pytest.mark.parametrize("bad", [-1, 2**64])
def test_u64_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        U64().process_bind_param(bad, DIALECT)


@pytest.mark.parametrize("bad", [1.5, Decimal("1"), "1", True])
def test_u64_rejects_non_int(bad: object) -> None:
    with pytest.raises(TypeError):
        U64().process_bind_param(bad, DIALECT)  # type: ignore[arg-type]


def test_u64_none_passthrough() -> None:
    assert U64().process_bind_param(None, DIALECT) is None
    assert U64().process_result_value(None, DIALECT) is None
