"""Builders for RPC responses (jsonParsed / base64 shapes) and a fake Solana reader."""

import base64
import struct
from typing import Any

from backend.blockchain.accounts import SPL_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID
from backend.integrations.helius import BudgetExceeded, RpcError
from backend.scanner.pumpfun import PUMP_FUN_PROGRAM_ID
from tests.fixtures.pumpportal import b58

SUPPLY = 1_000_000_000 * 10**6


def mint_account(
    *,
    token_2022: bool = False,
    mint_authority: str | None = None,
    freeze_authority: str | None = None,
    extensions: list[dict[str, Any]] | None = None,
    supply: int = SUPPLY,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "decimals": 6,
        "freezeAuthority": freeze_authority,
        "isInitialized": True,
        "mintAuthority": mint_authority,
        "supply": str(supply),
    }
    if extensions is not None:
        info["extensions"] = extensions
    program = TOKEN_2022_PROGRAM_ID if token_2022 else SPL_TOKEN_PROGRAM_ID
    return {
        "owner": program,
        "lamports": 1_461_600,
        "data": {
            "parsed": {"info": info, "type": "mint"},
            "program": "spl-token-2022" if token_2022 else "spl-token",
        },
    }


def curve_account(
    v_tokens: int = 900_000_000 * 10**6,
    v_sol: int = 40 * 10**9,
    r_tokens: int = 620_100_000 * 10**6,
    r_sol: int = 10 * 10**9,
    supply: int = SUPPLY,
    complete: bool = False,
    owner: str = PUMP_FUN_PROGRAM_ID,
) -> dict[str, Any]:
    raw = struct.pack("<8sQQQQQ?", b"\x17" * 8, v_tokens, v_sol, r_tokens, r_sol, supply, complete)
    raw += b"\x00" * 32  # trailing fields (e.g. creator) are ignored
    return {"owner": owner, "data": [base64.b64encode(raw).decode(), "base64"]}


def token_account(owner: str, mint: str) -> dict[str, Any]:
    return {
        "owner": SPL_TOKEN_PROGRAM_ID,
        "data": {"parsed": {"info": {"mint": mint, "owner": owner}, "type": "account"}},
    }


def signatures(slots: list[int], *, failed: set[int] | None = None) -> list[dict[str, Any]]:
    """Newest first, like the RPC. ``failed`` holds indexes of failed transactions."""
    failed = failed or set()
    return [
        {
            "signature": b58(88, 50_000 + i),
            "slot": slot,
            "err": {"x": 1} if i in failed else None,
            "blockTime": 1_763_000_000 + slot,
        }
        for i, slot in enumerate(slots)
    ]


def funding_tx(source: str, destination: str, *, inner: bool = False) -> dict[str, Any]:
    ix = {
        "program": "system",
        "programId": "11111111111111111111111111111111",
        "parsed": {
            "type": "transfer",
            "info": {"source": source, "destination": destination, "lamports": 10**8},
        },
    }
    message = {"instructions": [] if inner else [ix]}
    meta = {"err": None, "innerInstructions": [{"index": 0, "instructions": [ix]}] if inner else []}
    return {
        "slot": 1,
        "blockTime": 1_763_000_000,
        "meta": meta,
        "transaction": {"message": message},
    }


class FakeReader:
    """In-memory SolanaReader. Values may be exceptions to simulate RPC failures."""

    def __init__(self) -> None:
        self.accounts: dict[str, Any] = {}
        self.largest: dict[str, Any] = {}
        self.sigs: dict[str, Any] = {}
        self.txs: dict[str, Any] = {}
        self.calls: list[str] = []
        self.budget_left: int | None = None

    def _spend(self, method: str) -> None:
        self.calls.append(method)
        if self.budget_left is not None:
            if self.budget_left <= 0:
                raise BudgetExceeded(method)
            self.budget_left -= 1

    @staticmethod
    def _value(value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    async def get_account_info(
        self, address: str, encoding: str = "jsonParsed"
    ) -> dict[str, Any] | None:
        self._spend("getAccountInfo")
        result: dict[str, Any] | None = self._value(self.accounts.get(address))
        return result

    async def get_multiple_accounts(
        self, addresses: list[str], encoding: str = "jsonParsed"
    ) -> list[dict[str, Any] | None]:
        self._spend("getMultipleAccounts")
        return [self._value(self.accounts.get(a)) for a in addresses]

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        self._spend("getTokenLargestAccounts")
        result: list[dict[str, Any]] = self._value(self.largest.get(mint, []))
        return result

    async def get_signatures_for_address(
        self, address: str, limit: int = 1000, before: str | None = None
    ) -> list[dict[str, Any]]:
        self._spend("getSignaturesForAddress")
        all_sigs: list[dict[str, Any]] = self._value(self.sigs.get(address, []))
        start = 0
        if before is not None:
            start = next(i for i, s in enumerate(all_sigs) if s["signature"] == before) + 1
        return all_sigs[start : start + limit]

    async def get_transaction(
        self, signature: str, encoding: str = "json"
    ) -> dict[str, Any] | None:
        self._spend("getTransaction")
        result: dict[str, Any] | None = self._value(self.txs.get(signature))
        return result


RPC_DOWN = RpcError("getAccountInfo: HTTP 503")
