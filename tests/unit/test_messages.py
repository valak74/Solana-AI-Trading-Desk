from decimal import Decimal

import pytest

from backend.database.enums import TradeSide
from backend.scanner.messages import (
    ControlMessage,
    MessageError,
    PoolMigrated,
    TokenCreated,
    TradeObserved,
    decode_json,
    event_type_of,
    parse_message,
    price_sol_per_token,
    scale_to_int,
    sol_to_lamports,
    to_decimal,
)
from tests.fixtures.pumpportal import create_msg, migration_msg, mint, sig, trade_msg


def test_json_numbers_are_decoded_as_decimal() -> None:
    assert decode_json('{"a": 0.1}')["a"] == Decimal("0.1")


def test_create_message_is_normalised_to_integer_units() -> None:
    event = parse_message(create_msg(mint(1), sig(1)))
    assert isinstance(event, TokenCreated)
    assert event.mint == mint(1)
    assert event.sol_amount_lamports == 1_000_000_001  # 1.000000001 SOL, exact
    assert event.initial_buy_raw == 35_000_000_123_456  # 6 decimals
    assert event.v_sol_lamports == 31_010_000_000
    assert event.v_tokens_raw == 1_038_000_000_000_000
    assert event.market_cap_sol == Decimal("29.87")
    assert (event.name, event.symbol, event.pool) == ("Test Coin", "TEST", "pump")
    assert event_type_of(event) == "token_created"


@pytest.mark.parametrize(("tx_type", "side"), [("buy", TradeSide.BUY), ("sell", TradeSide.SELL)])
def test_trade_sides(tx_type: str, side: TradeSide) -> None:
    event = parse_message(trade_msg(mint(2), sig(2), tx_type))
    assert isinstance(event, TradeObserved)
    assert event.side is side
    assert event.token_amount_raw == 1_000_000_500_000
    assert event.sol_amount_lamports == 30_100_000


def test_optional_fields_may_be_missing() -> None:
    event = parse_message({"txType": "buy", "mint": mint(3), "signature": sig(3)})
    assert isinstance(event, TradeObserved)
    assert event.sol_amount_lamports is None
    assert event.pool == "pump"


def test_migration() -> None:
    event = parse_message(migration_msg(mint(4), sig(4)))
    assert isinstance(event, PoolMigrated)
    assert event_type_of(event) == "migration"


@pytest.mark.parametrize(
    "payload",
    [
        {"txType": "buy", "mint": "not-a-key", "signature": sig(5)},
        {"txType": "create", "mint": mint(5), "signature": "short"},
        {"txType": "sell", "signature": sig(5)},
    ],
)
def test_known_type_with_bad_identifiers_raises(payload: dict[str, object]) -> None:
    with pytest.raises(MessageError):
        parse_message(payload)


def test_control_and_unknown_messages() -> None:
    ack = parse_message({"message": "Successfully subscribed to token creation events."})
    assert isinstance(ack, ControlMessage)
    assert parse_message({"something": "else"}) is None
    assert parse_message(["not", "a", "dict"]) is None
    assert event_type_of(None) == "unknown"


def test_floats_are_refused_to_force_decimal_decoding() -> None:
    assert to_decimal(1.5) is None
    assert to_decimal(True) is None
    assert to_decimal("1.5") == Decimal("1.5")
    assert to_decimal(Decimal("NaN")) is None


def test_rounding_is_half_even_beyond_precision() -> None:
    assert sol_to_lamports(Decimal("0.0000000005")) == 0
    assert sol_to_lamports(Decimal("0.0000000015")) == 2
    assert scale_to_int(Decimal("1.0000005"), 6) == 1_000_000


def test_bonding_curve_price() -> None:
    price = price_sol_per_token(30 * 10**9, 1_073_000_000 * 10**6)
    assert price == Decimal("0.000000027958993476")
    assert price_sol_per_token(None, 1) is None
    assert price_sol_per_token(1, 0) is None
