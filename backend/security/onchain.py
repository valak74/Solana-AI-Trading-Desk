"""On-chain data collection for the security analysis (all reads go through the RPC budget)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from backend.blockchain.accounts import token_account_owner

SIGNATURE_PAGE = 1000
_FUNDING_INSTRUCTIONS = {"transfer", "transferWithSeed", "createAccount", "createAccountWithSeed"}


class SolanaReader(Protocol):
    async def get_account_info(
        self, address: str, encoding: str = "jsonParsed"
    ) -> dict[str, Any] | None: ...

    async def get_multiple_accounts(
        self, addresses: list[str], encoding: str = "jsonParsed"
    ) -> list[dict[str, Any] | None]: ...

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]: ...

    async def get_signatures_for_address(
        self, address: str, limit: int = 1000, before: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_transaction(
        self, signature: str, encoding: str = "json"
    ) -> dict[str, Any] | None: ...


async def holder_shares(
    rpc: SolanaReader, mint: str, supply_raw: int, exclude_owners: set[str]
) -> dict[str, Decimal]:
    """Owner wallet -> share of total supply, for the largest token accounts (max 20)."""
    if supply_raw <= 0:
        return {}
    largest = await rpc.get_token_largest_accounts(mint)
    addresses = [a["address"] for a in largest if isinstance(a.get("address"), str)]
    accounts = await rpc.get_multiple_accounts(addresses) if addresses else []
    by_address = dict(zip(addresses, accounts, strict=True))
    shares: dict[str, Decimal] = {}
    for entry in largest:
        owner = token_account_owner(by_address.get(entry.get("address", "")))
        try:
            amount = int(entry.get("amount", 0))
        except (TypeError, ValueError):
            continue
        if owner is None or owner in exclude_owners or amount <= 0:
            continue
        shares[owner] = shares.get(owner, Decimal(0)) + Decimal(amount) / Decimal(supply_raw)
    return {k: v.quantize(Decimal("0.000001")) for k, v in shares.items()}


def find_funder(tx: dict[str, Any] | None, wallet: str) -> str | None:
    """Source of the first system transfer/creation that funded ``wallet`` in a parsed tx."""
    if tx is None:
        return None
    message = (tx.get("transaction") or {}).get("message") or {}
    instructions = list(message.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(inner.get("instructions") or [])
    for ix in instructions:
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or ix.get("program") != "system":
            continue
        info = parsed.get("info") or {}
        if parsed.get("type") not in _FUNDING_INSTRUCTIONS:
            continue
        target = info.get("destination") or info.get("newAccount")
        source = info.get("source")
        if target == wallet and isinstance(source, str) and source != wallet:
            return source
    return None


@dataclass(frozen=True, slots=True)
class WalletProfile:
    address: str
    tx_count: int  # capped at the query limit
    complete_history: bool
    first_seen: datetime | None
    fresh: bool
    funder: str | None


async def profile_wallet(
    rpc: SolanaReader, address: str, max_txs: int, max_age: timedelta, now: datetime
) -> WalletProfile:
    """A wallet is *fresh* when its whole history is short and recent.

    Only fresh wallets get a funder lookup: established wallets are not the typical sybil
    pattern, and walking long histories would waste credits.
    """
    signatures = await rpc.get_signatures_for_address(address, limit=max_txs)
    complete = len(signatures) < max_txs
    oldest = signatures[-1] if signatures else None
    block_time = oldest.get("blockTime") if oldest else None
    first_seen = datetime.fromtimestamp(block_time, UTC) if isinstance(block_time, int) else None
    fresh = complete and (first_seen is None or now - first_seen <= max_age)
    funder = None
    if fresh and oldest is not None and isinstance(oldest.get("signature"), str):
        funder = find_funder(await rpc.get_transaction(oldest["signature"], "jsonParsed"), address)
    return WalletProfile(address, len(signatures), complete, first_seen, fresh, funder)


async def creation_slot_activity(
    rpc: SolanaReader, curve_address: str, max_pages: int
) -> int | None:
    """Successful transactions in the bonding curve's first slot, creation excluded.

    Returns None when the history is too long to reach the creation within ``max_pages``.
    """
    collected: list[dict[str, Any]] = []
    before: str | None = None
    for _ in range(max_pages):
        page = await rpc.get_signatures_for_address(curve_address, SIGNATURE_PAGE, before)
        collected.extend(page)
        if len(page) < SIGNATURE_PAGE:
            break
        before = page[-1].get("signature")
    else:
        return None
    if not collected:
        return None
    creation_slot = collected[-1].get("slot")
    in_slot = [s for s in collected if s.get("slot") == creation_slot and s.get("err") is None]
    return max(0, len(in_slot) - 1)
