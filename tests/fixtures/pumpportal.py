"""Synthetic PumpPortal-style messages, decoded the way the provider decodes them (Decimal)."""

import json
import random
from decimal import Decimal
from typing import Any

from backend.scanner.messages import decode_json

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(length: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(_ALPHABET) for _ in range(length))


def mint(seed: int) -> str:
    return b58(44, seed)


def sig(seed: int) -> str:
    return b58(88, 10_000 + seed)


def as_decoded(payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through JSON text so numbers become Decimal exactly as in production."""
    decoded: dict[str, Any] = decode_json(json.dumps(payload, default=str))
    return decoded


def create_text(m: str, s: str, **extra: Any) -> str:
    payload = {
        "signature": s,
        "mint": m,
        "traderPublicKey": b58(44, 999),
        "txType": "create",
        "initialBuy": 35_000_000.123456,
        "solAmount": 1.000000001,
        "bondingCurveKey": b58(44, 777),
        "vTokensInBondingCurve": 1_038_000_000,
        "vSolInBondingCurve": 31.01,
        "marketCapSol": 29.87,
        "name": "Test Coin",
        "symbol": "TEST",
        "uri": "https://example.invalid/meta.json",
        "pool": "pump",
    } | extra
    return json.dumps(payload)


def create_msg(m: str, s: str, **extra: Any) -> dict[str, Any]:
    decoded: dict[str, Any] = decode_json(create_text(m, s, **extra))
    return decoded


def trade_msg(m: str, s: str, side: str = "buy", **extra: Any) -> dict[str, Any]:
    payload = {
        "signature": s,
        "mint": m,
        "traderPublicKey": b58(44, 555),
        "txType": side,
        "tokenAmount": 1_000_000.5,
        "solAmount": 0.0301,
        "newTokenBalance": 1_000_000.5,
        "bondingCurveKey": b58(44, 777),
        "vTokensInBondingCurve": 1_037_000_000,
        "vSolInBondingCurve": 31.04,
        "marketCapSol": 29.93,
        "pool": "pump",
    } | extra
    decoded: dict[str, Any] = decode_json(json.dumps(payload))
    return decoded


def migration_msg(m: str, s: str) -> dict[str, Any]:
    decoded: dict[str, Any] = decode_json(
        json.dumps({"signature": s, "mint": m, "txType": "migrate", "pool": "pump"})
    )
    return decoded


ONE_SOL = Decimal(1)
