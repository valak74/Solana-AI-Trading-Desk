"""Connection test for the PumpPortal feed, to run once on the operator's machine.

    python -m backend.scanner.probe --seconds 60 --onchain 3

Connects with the configured API key, listens without touching the database, and prints what
was received: message counts per type, parse failures, the field names seen for each type, and
any server control messages. It validates the message format assumed by the parser.

With ``--onchain N`` (needs HELIUS_API_KEY, about 2 credits per token), it also reads the mint
and bonding-curve accounts of N new tokens and prints how they decode, which validates the
on-chain layouts assumed by the security analysis.

With ``--jupiter`` (needs JUPITER_API_KEY), it requests two quotes: SOL -> USDC, to validate the
response format, and SOL -> one freshly created token, to learn whether Jupiter routes tokens
that are still on the pump.fun bonding curve.
"""

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from typing import Any

import httpx

from backend.blockchain.accounts import AccountDecodeError, parse_bonding_curve, parse_mint
from backend.config import Settings, get_settings
from backend.integrations.helius import CreditBudget, HeliusRpc, RpcError
from backend.integrations.jupiter import WSOL_MINT, JupiterClient
from backend.monitoring.logging import configure_logging
from backend.scanner.messages import MessageError, event_type_of, parse_message
from backend.scanner.providers.base import ConnectionStatus, RawMessage
from backend.scanner.providers.pumpportal import PumpPortalProvider


async def decode_onchain(settings: Settings, tokens: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Read and decode mint + bonding curve accounts of a few live tokens."""
    if settings.helius_api_key is None:
        return [{"error": "HELIUS_API_KEY not set"}]
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=15) as client:
        rpc = HeliusRpc(
            client,
            settings.helius_rpc_url,
            settings.helius_api_key.get_secret_value(),
            CreditBudget(50),
        )
        for mint_address, curve_address in tokens:
            entry: dict[str, Any] = {"mint": mint_address}
            try:
                mint = parse_mint(await rpc.get_account_info(mint_address))
                entry["mint_program"] = "token-2022" if mint.is_token_2022 else "spl-token"
                entry["decimals"] = mint.decimals
                entry["mint_authority_revoked"] = mint.mint_authority is None
                entry["freeze_authority_revoked"] = mint.freeze_authority is None
                entry["extensions"] = [e.name for e in mint.extensions]
            except (RpcError, httpx.HTTPError, AccountDecodeError) as exc:
                entry["mint_error"] = f"{type(exc).__name__}: {exc}"
            try:
                curve = parse_bonding_curve(await rpc.get_account_info(curve_address, "base64"))
                entry["curve"] = {
                    "virtual_sol": curve.virtual_sol_reserves,
                    "virtual_tokens": curve.virtual_token_reserves,
                    "real_sol": curve.real_sol_reserves,
                    "real_tokens": curve.real_token_reserves,
                    "total_supply": curve.token_total_supply,
                    "complete": curve.complete,
                }
            except (RpcError, httpx.HTTPError, AccountDecodeError) as exc:
                entry["curve_error"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
    return results


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


async def check_jupiter(settings: Settings, fresh_mint: str | None) -> dict[str, Any]:
    if settings.jupiter_api_key is None:
        return {"error": "JUPITER_API_KEY not set"}
    report: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        key = settings.jupiter_api_key.get_secret_value()
        params = {"inputMint": WSOL_MINT, "outputMint": USDC_MINT, "amount": "10000000"}
        try:
            raw = await client.get(
                f"{settings.jupiter_base_url.rstrip('/')}/order",
                params=params,
                headers={"x-api-key": key},
            )
            report["usdc_http_status"] = raw.status_code
            body = (
                raw.json()
                if raw.headers.get("content-type", "").startswith("application/json")
                else None
            )
            report["usdc_response_fields"] = sorted(body) if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError) as exc:
            report["usdc_error"] = type(exc).__name__
        jupiter = JupiterClient(client, key, settings.jupiter_base_url)
        usdc = await jupiter.quote(WSOL_MINT, USDC_MINT, 10_000_000)
        report["usdc_parsed"] = (
            None if usdc is None else {"out_amount": usdc.out_amount, "fee_bps": usdc.fee_bps}
        )
        if fresh_mint is not None:
            fresh = await jupiter.buy_quote(fresh_mint, 10_000_000)
            report["bonding_curve_token_routable"] = fresh is not None
            if fresh is not None:
                report["bonding_curve_quote"] = {
                    "out_amount": fresh.out_amount,
                    "fee_bps": fresh.fee_bps,
                }
    return report


async def probe(
    seconds: float, track_first: int, onchain: int = 0, jupiter: bool = False
) -> dict[str, Any]:
    settings = get_settings()
    key = settings.pumpportal_api_key.get_secret_value() if settings.pumpportal_api_key else None
    provider = PumpPortalProvider(settings.pumpportal_ws_url, key)
    counts: Counter[str] = Counter()
    fields: defaultdict[str, set[str]] = defaultdict(set)
    controls: list[str] = []
    connections: list[str] = []
    tracked = 0
    created: list[tuple[str, str]] = []

    async def listen() -> None:
        nonlocal tracked
        async for item in provider.items():
            if isinstance(item, ConnectionStatus):
                connections.append("connected" if item.connected else f"lost:{item.reason}")
                continue
            if not isinstance(item, RawMessage):
                continue
            try:
                event = parse_message(item.payload)
                kind = event_type_of(event)
            except MessageError:
                kind = "malformed"
            counts[kind] += 1
            fields[kind].update(item.payload)
            if kind == "control":
                controls.append(str(item.payload)[:300])
            mint = item.payload.get("mint")
            curve = item.payload.get("bondingCurveKey")
            if kind == "token_created" and isinstance(mint, str) and isinstance(curve, str):
                created.append((mint, curve))
            if kind == "token_created" and tracked < track_first and isinstance(mint, str):
                await provider.track([mint])
                tracked += 1

    with_timeout = asyncio.wait_for(listen(), timeout=seconds)
    try:
        await with_timeout
    except TimeoutError:
        pass
    finally:
        await provider.close()
    return {
        "seconds": seconds,
        "connections": connections,
        "messages_by_type": dict(counts),
        "fields_by_type": {k: sorted(v) for k, v in fields.items()},
        "control_messages": controls[:10],
        "tokens_tracked_for_trades": tracked,
        "onchain_decoding": await decode_onchain(settings, created[:onchain]) if onchain else [],
        "jupiter": await check_jupiter(settings, created[0][0] if created else None)
        if jupiter
        else {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="PumpPortal feed connection test")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--track", type=int, default=5, help="new tokens to follow for trades")
    parser.add_argument("--onchain", type=int, default=0, help="new tokens to decode on-chain")
    parser.add_argument("--jupiter", action="store_true", help="validate Jupiter quotes")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging("WARNING", "console", settings.secret_values())
    report = asyncio.run(probe(args.seconds, args.track, args.onchain, args.jupiter))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
