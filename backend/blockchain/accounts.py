"""Decoding of on-chain accounts read through the RPC.

Mints are requested with ``jsonParsed`` encoding: the RPC node decodes SPL Token and Token-2022
layouts (including extensions), which is far less error-prone than parsing bytes ourselves.
The pump.fun bonding curve is a program account, decoded from base64 with strict sanity checks.
"""

import base64
import struct
from dataclasses import dataclass, field
from typing import Any

from backend.scanner.pumpfun import PUMP_FUN_PROGRAM_ID

SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105 - public id
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"  # noqa: S105 - public id
TOKEN_PROGRAMS = {SPL_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}


class AccountDecodeError(ValueError):
    """The account is missing, owned by an unexpected program, or has an unexpected layout."""


@dataclass(frozen=True, slots=True)
class Extension:
    name: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MintInfo:
    program_id: str
    supply_raw: int
    decimals: int
    mint_authority: str | None
    freeze_authority: str | None
    extensions: tuple[Extension, ...]

    @property
    def is_token_2022(self) -> bool:
        return self.program_id == TOKEN_2022_PROGRAM_ID


def _dict_or_empty(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_mint(account: dict[str, Any] | None) -> MintInfo:
    if account is None:
        raise AccountDecodeError("mint account not found")
    owner = account.get("owner")
    if owner not in TOKEN_PROGRAMS:
        raise AccountDecodeError("mint not owned by a token program")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        raise AccountDecodeError("account is not a parsed mint")
    info = parsed.get("info") or {}
    try:
        supply = int(info["supply"])
        decimals = int(info["decimals"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountDecodeError("mint supply/decimals unreadable") from exc
    extensions = tuple(
        Extension(str(e.get("extension")), _dict_or_empty(e.get("state")))
        for e in info.get("extensions") or []
        if isinstance(e, dict)
    )
    return MintInfo(
        program_id=str(owner),
        supply_raw=supply,
        decimals=decimals,
        mint_authority=info.get("mintAuthority") or None,
        freeze_authority=info.get("freezeAuthority") or None,
        extensions=extensions,
    )


@dataclass(frozen=True, slots=True)
class BondingCurveState:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool


_CURVE_LAYOUT = struct.Struct("<8sQQQQQ?")  # discriminator, 5 x u64, complete flag


def parse_bonding_curve(account: dict[str, Any] | None) -> BondingCurveState:
    if account is None:
        raise AccountDecodeError("bonding curve account not found")
    if account.get("owner") != PUMP_FUN_PROGRAM_ID:
        raise AccountDecodeError("bonding curve not owned by the pump.fun program")
    data = account.get("data")
    if not (isinstance(data, list) and len(data) == 2 and data[1] == "base64"):
        raise AccountDecodeError("bonding curve data not base64-encoded")
    raw = base64.b64decode(data[0])
    if len(raw) < _CURVE_LAYOUT.size:
        raise AccountDecodeError("bonding curve data too short")
    _, v_tokens, v_sol, r_tokens, r_sol, supply, complete = _CURVE_LAYOUT.unpack_from(raw)
    state = BondingCurveState(v_tokens, v_sol, r_tokens, r_sol, supply, complete)
    if not (supply > 0 and v_tokens >= r_tokens and v_sol >= r_sol and r_tokens <= supply):
        raise AccountDecodeError("bonding curve values fail sanity checks")
    return state


def token_account_owner(account: dict[str, Any] | None) -> str | None:
    if account is None:
        return None
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    owner = info.get("owner") if isinstance(info, dict) else None
    return owner if isinstance(owner, str) else None
