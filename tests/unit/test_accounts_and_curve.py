from decimal import Decimal

import pytest

from backend.blockchain.accounts import (
    AccountDecodeError,
    parse_bonding_curve,
    parse_mint,
    token_account_owner,
)
from backend.blockchain.curve_math import buy_tokens_out, estimate_round_trip, sell_sol_out
from tests.fixtures.chain import SUPPLY, curve_account, mint_account, token_account
from tests.fixtures.pumpportal import mint


def test_parse_spl_mint() -> None:
    info = parse_mint(mint_account())
    assert (info.supply_raw, info.decimals) == (SUPPLY, 6)
    assert info.mint_authority is None and info.freeze_authority is None
    assert info.extensions == () and not info.is_token_2022


def test_parse_token_2022_mint_with_extensions() -> None:
    account = mint_account(
        token_2022=True,
        mint_authority=mint(1),
        extensions=[
            {"extension": "metadataPointer", "state": {"authority": None}},
            {"extension": "nonTransferable"},
        ],
    )
    info = parse_mint(account)
    assert info.is_token_2022 and info.mint_authority == mint(1)
    assert [e.name for e in info.extensions] == ["metadataPointer", "nonTransferable"]
    assert info.extensions[1].state == {}


@pytest.mark.parametrize(
    "account",
    [
        None,
        mint_account() | {"owner": "11111111111111111111111111111111"},
        {"owner": mint_account()["owner"], "data": {"parsed": {"type": "account", "info": {}}}},
        {"owner": mint_account()["owner"], "data": {"parsed": {"type": "mint", "info": {}}}},
    ],
)
def test_parse_mint_rejects_bad_accounts(account: dict[str, object] | None) -> None:
    with pytest.raises(AccountDecodeError):
        parse_mint(account)


def test_parse_bonding_curve() -> None:
    state = parse_bonding_curve(curve_account(complete=True))
    assert state.virtual_sol_reserves == 40 * 10**9
    assert state.real_token_reserves == 620_100_000 * 10**6
    assert state.complete


@pytest.mark.parametrize(
    "account",
    [
        None,
        curve_account(owner="11111111111111111111111111111111"),  # spoofed account
        {"owner": curve_account()["owner"], "data": ["AAAA", "base64"]},  # too short
        curve_account(v_tokens=1, r_tokens=2),  # virtual < real: impossible state
        {"owner": curve_account()["owner"], "data": {"parsed": {}}},
    ],
)
def test_parse_bonding_curve_rejects_bad_accounts(account: dict[str, object] | None) -> None:
    with pytest.raises(AccountDecodeError):
        parse_bonding_curve(account)


def test_token_account_owner() -> None:
    assert token_account_owner(token_account(mint(2), mint(1))) == mint(2)
    assert token_account_owner(None) is None
    assert token_account_owner({"data": ["x", "base64"]}) is None


def test_curve_math_round_trip_costs_at_least_the_fees() -> None:
    v_sol, v_tokens = 30 * 10**9, 1_073_000_000 * 10**6
    small = estimate_round_trip(v_sol, v_tokens, 10**7, 125)
    large = estimate_round_trip(v_sol, v_tokens, 3 * 10**9, 125)
    assert small is not None and large is not None
    fees_only = 1 - (Decimal("0.9875") ** 2)  # two 1.25 % fees
    assert abs(small.round_trip_cost - fees_only) < Decimal("0.0001")
    # The entry impact grows with size, but an immediate round trip recovers it: only fees lost.
    assert large.entry_price_impact > small.entry_price_impact
    assert large.round_trip_cost == small.round_trip_cost
    assert estimate_round_trip(0, v_tokens, 10**7, 125) is None


def test_buy_and_sell_are_consistent_without_fees() -> None:
    v_sol, v_tokens = 30 * 10**9, 1_073_000_000 * 10**6
    tokens = buy_tokens_out(v_sol, v_tokens, 10**9, 0)
    back = sell_sol_out(v_sol + 10**9, v_tokens - tokens, tokens, 0)
    assert 10**9 - 2 <= back <= 10**9  # only integer rounding is lost
