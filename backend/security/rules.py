"""Deterministic security rules. Each rule only adds findings; none can remove one.

Principle: anything that cannot be assessed is treated as a risk, never as safe. An unknown
Token-2022 extension is critical because its effect on transfers is unknown.
"""

from collections import Counter
from decimal import Decimal

from backend.blockchain.accounts import BondingCurveState, MintInfo
from backend.blockchain.curve_math import TradeCostEstimate
from backend.security.config import SecurityThresholds
from backend.security.findings import Findings

# Extensions with no effect on the ability to sell (display/metadata only).
BENIGN_EXTENSIONS = {
    "metadataPointer",
    "tokenMetadata",
    "groupPointer",
    "tokenGroup",
    "groupMemberPointer",
    "tokenGroupMember",
}
# Extensions that do not block selling but deserve attention.
WARNING_EXTENSIONS = {
    "mintCloseAuthority",
    "interestBearingConfig",
    "scaledUiAmountConfig",
    "confidentialTransferMint",
    "confidentialTransferFeeConfig",
}


def check_mint(mint: MintInfo, t: SecurityThresholds, f: Findings) -> None:
    if mint.mint_authority is not None:
        f.add_critical("mint_authority_active")
    if mint.freeze_authority is not None:
        f.add_critical("freeze_authority_active")
    f.metrics["token_program"] = "token-2022" if mint.is_token_2022 else "spl-token"
    f.metrics["extensions"] = sorted(e.name for e in mint.extensions)
    for ext in mint.extensions:
        _check_extension(ext.name, ext.state, t, f)


def _max_transfer_fee_bps(state: dict[str, object]) -> int:
    fees = []
    for key in ("olderTransferFee", "newerTransferFee"):
        fee = state.get(key)
        if isinstance(fee, dict):
            try:
                fees.append(int(fee.get("transferFeeBasisPoints", 0)))
            except (TypeError, ValueError):
                fees.append(10_000)  # unreadable: assume the worst
    return max(fees, default=10_000)


def _check_extension(
    name: str, state: dict[str, object], t: SecurityThresholds, f: Findings
) -> None:
    if name in BENIGN_EXTENSIONS:
        return
    if name in WARNING_EXTENSIONS:
        f.add_warning(f"token2022_{name}")
        return
    if name == "transferFeeConfig":
        bps = _max_transfer_fee_bps(state)
        f.metrics["transfer_fee_bps"] = bps
        if bps >= t.transfer_fee_critical_bps:
            f.add_critical("token2022_transfer_fee_high")
        elif bps >= t.transfer_fee_warning_bps:
            f.add_warning("token2022_transfer_fee")
    elif name == "permanentDelegate":
        if state.get("delegate"):
            f.add_critical("token2022_permanent_delegate")
    elif name == "transferHook":
        if state.get("programId"):
            f.add_critical("token2022_transfer_hook")
    elif name == "defaultAccountState":
        if state.get("accountState") != "initialized":
            f.add_critical("token2022_default_account_frozen")
    elif name == "pausableConfig":
        if state.get("authority") or state.get("paused"):
            f.add_critical("token2022_pausable")
    elif name == "nonTransferable":
        f.add_critical("token2022_non_transferable")
    else:
        f.add_critical(f"token2022_unknown_extension:{name}")


def check_curve(
    curve: BondingCurveState, cost: TradeCostEstimate | None, t: SecurityThresholds, f: Findings
) -> None:
    f.metrics["curve_complete"] = curve.complete
    f.metrics["real_sol_lamports"] = curve.real_sol_reserves
    sold = curve.token_total_supply - curve.real_token_reserves
    f.metrics["curve_tokens_sold_share"] = str(
        (Decimal(max(0, sold)) / Decimal(curve.token_total_supply)).quantize(Decimal("0.000001"))
    )
    if curve.complete:
        f.add_critical("curve_complete_not_tradable")
    if cost is None:
        f.add_missing("trade_cost_estimate")
        return
    f.metrics["entry_price_impact"] = str(cost.entry_price_impact)
    f.metrics["round_trip_cost"] = str(cost.round_trip_cost)
    # On a constant-product curve an immediate round trip only loses the fees (the entry
    # impact is recovered on exit), so the risk signal is the entry impact, not this cost.
    if cost.entry_price_impact > t.entry_impact_warning:
        f.add_warning("high_entry_price_impact")


def check_holders(
    shares: dict[str, Decimal], creator: str | None, t: SecurityThresholds, f: Findings
) -> None:
    """``shares``: owner wallet -> fraction of total supply, bonding curve already excluded."""
    ranked = sorted(shares.values(), reverse=True)
    top1 = ranked[0] if ranked else Decimal(0)
    top10 = sum(ranked[:10], Decimal(0))
    creator_share = shares.get(creator, Decimal(0)) if creator else Decimal(0)
    f.metrics |= {
        "top1_share": str(top1),
        "top10_share": str(top10),
        "creator_share": str(creator_share),
    }
    if top1 >= t.top1_share_critical:
        f.add_critical("top_holder_concentration")
    if top10 >= t.top10_share_critical:
        f.add_critical("top10_concentration")
    elif top10 >= t.top10_share_warning:
        f.add_warning("top10_concentration")
    if creator_share >= t.creator_share_critical:
        f.add_critical("creator_holds_large_share")
    elif creator_share >= t.creator_share_warning:
        f.add_warning("creator_holds_share")


def check_creator_history(recent_launches_24h: int, t: SecurityThresholds, f: Findings) -> None:
    f.metrics["creator_launches_24h"] = recent_launches_24h
    if recent_launches_24h >= t.serial_creator_critical_24h:
        f.add_critical("serial_creator")
    elif recent_launches_24h >= t.serial_creator_warning_24h:
        f.add_warning("serial_creator")


def check_creator_sold(creator: str | None, sellers: set[str], f: Findings) -> None:
    if creator is not None and creator in sellers:
        f.add_critical("creator_sold")


def check_bundle(creation_slot_txs: int | None, t: SecurityThresholds, f: Findings) -> None:
    if creation_slot_txs is None:
        f.add_missing("creation_slot_activity")
        return
    f.metrics["creation_slot_txs"] = creation_slot_txs
    if creation_slot_txs >= t.bundle_tx_critical:
        f.add_critical("bundled_launch")
    elif creation_slot_txs >= t.bundle_tx_warning:
        f.add_warning("bundled_launch")


def check_funding(
    funders: dict[str, str | None],
    fresh: dict[str, bool],
    creator: str | None,
    t: SecurityThresholds,
    f: Findings,
) -> None:
    """``funders``: profiled holder -> first funding wallet (None if unknown/established)."""
    known = [src for src in funders.values() if src]
    largest_cluster = max(Counter(known).values(), default=0)
    fresh_count = sum(1 for v in fresh.values() if v)
    f.metrics |= {
        "profiled_holders": len(funders),
        "fresh_holders": fresh_count,
        "largest_funding_cluster": largest_cluster,
    }
    if largest_cluster >= t.funding_cluster_critical:
        f.add_critical("coordinated_wallets")
    if creator is not None and creator in known:
        f.add_critical("creator_funded_holders")
    if funders and fresh_count * 2 >= len(funders):
        f.add_warning("mostly_fresh_holders")


def check_rugcheck(dangers: list[str], t: SecurityThresholds, f: Findings) -> None:
    f.metrics["rugcheck_dangers"] = dangers
    for name in dangers:
        flag = f"rugcheck:{name}"[:96]
        if t.rugcheck_danger_is_critical:
            f.add_critical(flag)
        else:
            f.add_warning(flag)
