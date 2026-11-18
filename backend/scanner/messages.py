"""Normalisation of PumpPortal data messages into typed events.

Field names follow PumpPortal's public examples (``signature``, ``mint``, ``traderPublicKey``,
``txType``, ``tokenAmount``, ``solAmount``, ``vSolInBondingCurve``, ...). They were not checked
against the official documentation from this environment, so parsing is deliberately tolerant:
only ``txType``/``mint``/``signature`` are required, every other field is optional, and messages
that cannot be parsed are still recorded raw (event type ``unknown``) for later re-processing.

JSON must be decoded with ``parse_float=Decimal`` (see ``decode_json``): amounts never go
through a binary float.
"""

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, TypeGuard

from backend.database.enums import TradeSide

LAMPORTS_PER_SOL = 10**9
PUMP_TOKEN_DECIMALS = 6

_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")


class MessageError(ValueError):
    """A message that claims a known type but is malformed."""


def decode_json(text: str | bytes) -> Any:
    return json.loads(text, parse_float=Decimal)


def is_pubkey(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 32 <= len(value) <= 44 and bool(_BASE58.match(value))


def is_signature(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 64 <= len(value) <= 88 and bool(_BASE58.match(value))


def to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int | str):
        try:
            result = Decimal(value)
        except InvalidOperation:
            return None
        return result if result.is_finite() else None
    return None  # floats are refused on purpose: decode JSON with decode_json()


def scale_to_int(value: object, decimals: int) -> int | None:
    """UI amount -> integer base units (e.g. SOL -> lamports), rounding half-even."""
    amount = to_decimal(value)
    if amount is None:
        return None
    return int((amount * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_HALF_EVEN))


def sol_to_lamports(value: object) -> int | None:
    return scale_to_int(value, 9)


@dataclass(frozen=True, slots=True)
class TokenCreated:
    signature: str
    mint: str
    creator: str | None
    name: str | None
    symbol: str | None
    uri: str | None
    pool: str
    bonding_curve: str | None
    initial_buy_raw: int | None
    sol_amount_lamports: int | None
    v_sol_lamports: int | None
    v_tokens_raw: int | None
    market_cap_sol: Decimal | None


@dataclass(frozen=True, slots=True)
class TradeObserved:
    signature: str
    mint: str
    trader: str | None
    side: TradeSide
    pool: str
    token_amount_raw: int | None
    sol_amount_lamports: int | None
    new_token_balance_raw: int | None
    bonding_curve: str | None
    v_sol_lamports: int | None
    v_tokens_raw: int | None
    market_cap_sol: Decimal | None


@dataclass(frozen=True, slots=True)
class PoolMigrated:
    signature: str
    mint: str
    pool: str


@dataclass(frozen=True, slots=True)
class ControlMessage:
    text: str


Event = TokenCreated | TradeObserved | PoolMigrated | ControlMessage

EVENT_TYPES: dict[type[Any], str] = {
    TokenCreated: "token_created",
    TradeObserved: "trade",
    PoolMigrated: "migration",
    ControlMessage: "control",
}


def event_type_of(event: Event | None) -> str:
    return "unknown" if event is None else EVENT_TYPES[type(event)]


def _opt_str(payload: dict[str, Any], key: str, max_len: int = 512) -> str | None:
    value = payload.get(key)
    return value[:max_len] if isinstance(value, str) and value else None


def _opt_pubkey(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if is_pubkey(value) else None


def _require_ids(payload: dict[str, Any]) -> tuple[str, str]:
    signature, mint = payload.get("signature"), payload.get("mint")
    if not is_signature(signature):
        raise MessageError("missing or invalid signature")
    if not is_pubkey(mint):
        raise MessageError("missing or invalid mint")
    return signature, mint


def _pool(payload: dict[str, Any]) -> str:
    return _opt_str(payload, "pool", 32) or "pump"


def _token_amount(payload: dict[str, Any], key: str) -> int | None:
    return scale_to_int(payload.get(key), PUMP_TOKEN_DECIMALS)


def _parse_create(payload: dict[str, Any]) -> TokenCreated:
    signature, mint = _require_ids(payload)
    return TokenCreated(
        signature=signature,
        mint=mint,
        creator=_opt_pubkey(payload, "traderPublicKey"),
        name=_opt_str(payload, "name", 256),
        symbol=_opt_str(payload, "symbol", 64),
        uri=_opt_str(payload, "uri", 1024),
        pool=_pool(payload),
        bonding_curve=_opt_pubkey(payload, "bondingCurveKey"),
        initial_buy_raw=_token_amount(payload, "initialBuy"),
        sol_amount_lamports=sol_to_lamports(payload.get("solAmount")),
        v_sol_lamports=sol_to_lamports(payload.get("vSolInBondingCurve")),
        v_tokens_raw=_token_amount(payload, "vTokensInBondingCurve"),
        market_cap_sol=to_decimal(payload.get("marketCapSol")),
    )


def _parse_trade(payload: dict[str, Any], side: TradeSide) -> TradeObserved:
    signature, mint = _require_ids(payload)
    return TradeObserved(
        signature=signature,
        mint=mint,
        trader=_opt_pubkey(payload, "traderPublicKey"),
        side=side,
        pool=_pool(payload),
        token_amount_raw=_token_amount(payload, "tokenAmount"),
        sol_amount_lamports=sol_to_lamports(payload.get("solAmount")),
        new_token_balance_raw=_token_amount(payload, "newTokenBalance"),
        bonding_curve=_opt_pubkey(payload, "bondingCurveKey"),
        v_sol_lamports=sol_to_lamports(payload.get("vSolInBondingCurve")),
        v_tokens_raw=_token_amount(payload, "vTokensInBondingCurve"),
        market_cap_sol=to_decimal(payload.get("marketCapSol")),
    )


def _parse_migration(payload: dict[str, Any]) -> PoolMigrated:
    signature, mint = _require_ids(payload)
    return PoolMigrated(signature=signature, mint=mint, pool=_pool(payload))


def parse_message(payload: object) -> Event | None:
    """Return a typed event, or None for an unrecognised message.

    Raises MessageError when a message has a known type but invalid identifiers.
    """
    if not isinstance(payload, dict):
        return None
    tx_type = payload.get("txType")
    if tx_type == "create":
        return _parse_create(payload)
    if tx_type == "buy":
        return _parse_trade(payload, TradeSide.BUY)
    if tx_type == "sell":
        return _parse_trade(payload, TradeSide.SELL)
    if tx_type == "migrate" or (tx_type is None and payload.get("method") == "migration"):
        return _parse_migration(payload)
    if tx_type is None and isinstance(payload.get("message"), str):
        return ControlMessage(text=payload["message"][:1000])
    if tx_type is None and isinstance(payload.get("errors"), str | list):
        return ControlMessage(text=str(payload["errors"])[:1000])
    return None


def price_sol_per_token(v_sol_lamports: int | None, v_tokens_raw: int | None) -> Decimal | None:
    """Marginal bonding-curve price, SOL per whole token, from virtual reserves."""
    if not v_sol_lamports or not v_tokens_raw or v_sol_lamports < 0 or v_tokens_raw < 0:
        return None
    sol = Decimal(v_sol_lamports) / LAMPORTS_PER_SOL
    tokens = Decimal(v_tokens_raw) / (Decimal(10) ** PUMP_TOKEN_DECIMALS)
    return (sol / tokens).quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN)
