from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from backend.analysis.engine import AnalysisTarget, TokenAnalyzer
from backend.analysis.flows import TradeFlow
from backend.integrations.helius import BudgetExceeded, CreditBudget, HeliusRpc
from backend.integrations.rugcheck import RugCheckClient
from backend.security.config import AnalysisConfig
from tests.fixtures.chain import (
    RPC_DOWN,
    SUPPLY,
    FakeReader,
    curve_account,
    funding_tx,
    mint_account,
    signatures,
    token_account,
)
from tests.fixtures.pumpportal import mint, sig

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
TOKEN, CURVE, CREATOR = mint(1), mint(2), mint(3)
HOLDERS = [mint(20 + i) for i in range(5)]


def target() -> AnalysisTarget:
    return AnalysisTarget(1, TOKEN, CREATOR, CURVE, NOW - timedelta(minutes=5))


def healthy_flow() -> TradeFlow:
    return TradeFlow(
        n_buys=60,
        n_sells=10,
        unique_buyers=45,
        unique_sellers=8,
        buy_lamports=30 * 10**9,
        sell_lamports=5 * 10**9,
        buy_ratio=Decimal("0.857"),
        acceleration=Decimal("2.2"),
        wash_ratio=Decimal("0.05"),
    )


def healthy_chain() -> FakeReader:
    reader = FakeReader()
    reader.accounts[TOKEN] = mint_account()
    reader.accounts[CURVE] = curve_account(v_sol=80 * 10**9, r_sol=50 * 10**9)
    reader.largest[TOKEN] = [{"address": mint(40), "amount": str(SUPPLY // 2)}] + [
        {"address": mint(41 + i), "amount": str(SUPPLY // 100)} for i in range(5)
    ]
    reader.accounts[mint(40)] = token_account(CURVE, TOKEN)
    for i, holder in enumerate(HOLDERS):
        reader.accounts[mint(41 + i)] = token_account(holder, TOKEN)
        reader.sigs[holder] = signatures(list(range(1000, 980, -1)))  # established wallets
    reader.sigs[CURVE] = signatures([105, 101, 100, 100])  # one tx alongside the creation
    return reader


def analyzer(reader: FakeReader, rugcheck: RugCheckClient | None = None) -> TokenAnalyzer:
    return TokenAnalyzer(reader, AnalysisConfig(), rugcheck, clock=lambda: NOW)


async def test_healthy_token_gets_deep_analysis_and_good_score() -> None:
    reader = healthy_chain()
    result = await analyzer(reader).analyze(target(), healthy_flow(), creator_launches_24h=0)
    f = result.findings
    assert f.critical == [] and f.missing == []
    assert result.deep_done
    assert f.metrics["creation_slot_txs"] == 1
    assert Decimal(f.metrics["top10_share"]) == Decimal("0.05")
    assert result.final.total >= Decimal(75)
    assert result.decision in {"candidate", "priority"}


async def test_critical_flag_skips_deep_analysis() -> None:
    reader = healthy_chain()
    reader.accounts[TOKEN] = mint_account(mint_authority=mint(9))
    result = await analyzer(reader).analyze(target(), healthy_flow(), 0)
    assert "mint_authority_active" in result.findings.critical
    assert not result.deep_done and result.decision == "reject"
    assert "getSignaturesForAddress" not in reader.calls  # no credits spent on deep checks


async def test_unreadable_mint_is_never_considered_safe() -> None:
    reader = healthy_chain()
    reader.accounts[TOKEN] = RPC_DOWN
    result = await analyzer(reader).analyze(target(), healthy_flow(), 0)
    assert {"mint_unverifiable"} <= set(result.findings.critical)
    assert {"mint_account", "holders"} <= set(result.findings.missing)
    assert result.decision == "reject"


async def test_spoofed_curve_is_critical() -> None:
    reader = healthy_chain()
    reader.accounts[CURVE] = curve_account(owner="11111111111111111111111111111111")
    result = await analyzer(reader).analyze(target(), healthy_flow(), 0)
    assert "curve_unverifiable" in result.findings.critical


async def test_weak_token_stays_at_watch_without_deep_analysis() -> None:
    reader = healthy_chain()
    weak = TradeFlow(
        n_buys=5,
        n_sells=5,
        unique_buyers=4,
        unique_sellers=4,
        buy_ratio=Decimal("0.5"),
        acceleration=Decimal("0.5"),
        wash_ratio=Decimal("0.6"),
    )
    result = await analyzer(reader).analyze(target(), weak, 0)
    assert not result.deep_done
    assert result.decision == "reject"


async def test_coordinated_holders_detected_in_deep_analysis() -> None:
    reader, funder = healthy_chain(), mint(99)
    for i, holder in enumerate(HOLDERS[:3]):
        reader.sigs[holder] = [
            {"signature": sig(200 + i), "slot": 1, "blockTime": int(NOW.timestamp()) - 600}
        ]
        reader.txs[sig(200 + i)] = funding_tx(funder, holder)
    result = await analyzer(reader).analyze(target(), healthy_flow(), 0)
    assert "coordinated_wallets" in result.findings.critical
    assert result.decision == "reject"


async def test_budget_exhaustion_propagates() -> None:
    reader = healthy_chain()
    reader.budget_left = 2
    with pytest.raises(BudgetExceeded):
        await analyzer(reader).analyze(target(), healthy_flow(), 0)


async def test_rugcheck_dangers_become_warnings() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "score_normalised": 70,
                "risks": [
                    {"name": "Low Liquidity", "level": "danger"},
                    {"name": "Info", "level": "info"},
                ],
            },
        )

    rugcheck = RugCheckClient(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result = await analyzer(healthy_chain(), rugcheck).analyze(target(), healthy_flow(), 0)
    assert "rugcheck:Low Liquidity" in result.findings.warnings


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"no": "risks"}),
    ],
)
async def test_rugcheck_failures_return_none(response: httpx.Response) -> None:
    client = RugCheckClient(httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)))
    assert await client.dangers(TOKEN) is None


# --- credit budget -----------------------------------------------------------------------


def test_budget_charges_and_resets_daily() -> None:
    now = [NOW]
    budget = CreditBudget(3, {"getTokenLargestAccounts": 2}, clock=lambda: now[0])
    budget.charge("getAccountInfo")
    budget.charge("getTokenLargestAccounts")
    with pytest.raises(BudgetExceeded):
        budget.charge("getAccountInfo")
    assert budget.remaining() == 0 and budget.calls["getAccountInfo"] == 1
    now[0] = NOW + timedelta(days=1)
    assert budget.remaining() == 3


async def test_rpc_does_not_send_when_budget_exhausted() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": None}})

    rpc = HeliusRpc(
        httpx.AsyncClient(transport=httpx.MockTransport(handle)), "https://x", "k", CreditBudget(1)
    )
    assert await rpc.get_account_info(TOKEN) is None
    with pytest.raises(BudgetExceeded):
        await rpc.get_account_info(TOKEN)
    assert len(sent) == 1
