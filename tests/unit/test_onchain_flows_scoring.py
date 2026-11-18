from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.analysis.flows import compute_trade_flow
from backend.database.enums import TradeSide
from backend.scanner.messages import TradeObserved
from backend.scoring.scorer import ScoreInputs, band_for, ramp, score
from backend.security.config import AnalysisConfig
from backend.security.onchain import (
    creation_slot_activity,
    find_funder,
    holder_shares,
    profile_wallet,
)
from tests.fixtures.chain import SUPPLY, FakeReader, funding_tx, signatures, token_account
from tests.fixtures.pumpportal import mint, sig

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
CFG = AnalysisConfig()

# --- on-chain collectors -----------------------------------------------------------------


async def test_holder_shares_excludes_curve_and_merges_owners() -> None:
    reader, token, curve = FakeReader(), mint(1), mint(2)
    accounts = [mint(10), mint(11), mint(12)]
    reader.largest[token] = [
        {"address": accounts[0], "amount": str(SUPPLY // 2)},  # curve's own token account
        {"address": accounts[1], "amount": str(SUPPLY // 10)},
        {"address": accounts[2], "amount": str(SUPPLY // 20)},
    ]
    reader.accounts |= {
        accounts[0]: token_account(curve, token),
        accounts[1]: token_account(mint(3), token),
        accounts[2]: token_account(mint(3), token),  # same wallet, second account
    }
    shares = await holder_shares(reader, token, SUPPLY, {curve})
    assert shares == {mint(3): Decimal("0.15")}


def test_find_funder_outer_and_inner_instructions() -> None:
    wallet, source = mint(1), mint(2)
    assert find_funder(funding_tx(source, wallet), wallet) == source
    assert find_funder(funding_tx(source, wallet, inner=True), wallet) == source
    assert find_funder(funding_tx(source, mint(3)), wallet) is None
    assert find_funder(None, wallet) is None


async def test_profile_wallet_fresh_with_funder() -> None:
    reader, wallet, funder = FakeReader(), mint(1), mint(2)
    reader.sigs[wallet] = [
        {"signature": sig(1), "slot": 5, "blockTime": int((NOW - timedelta(hours=2)).timestamp())}
    ]
    reader.txs[sig(1)] = funding_tx(funder, wallet)
    profile = await profile_wallet(reader, wallet, 20, timedelta(hours=24), NOW)
    assert profile.fresh and profile.funder == funder and profile.complete_history


async def test_profile_wallet_established_skips_funder_lookup() -> None:
    reader, wallet = FakeReader(), mint(1)
    reader.sigs[wallet] = signatures(list(range(100, 80, -1)))  # 20 = limit: history continues
    profile = await profile_wallet(reader, wallet, 20, timedelta(hours=24), NOW)
    assert not profile.fresh and profile.funder is None
    assert "getTransaction" not in reader.calls


async def test_creation_slot_activity() -> None:
    reader, curve = FakeReader(), mint(1)
    # newest first; the oldest (creation) is in slot 100, with 3 more successful txs + 1 failed
    reader.sigs[curve] = signatures([105, 103, 100, 100, 100, 100, 100], failed={3})
    assert await creation_slot_activity(reader, curve, max_pages=3) == 3


async def test_creation_slot_unknown_when_history_too_long() -> None:
    reader, curve = FakeReader(), mint(1)
    reader.sigs[curve] = signatures([200] * 2500)
    assert await creation_slot_activity(reader, curve, max_pages=2) is None
    assert await creation_slot_activity(reader, curve, max_pages=3) == 2499


# --- trade flows -------------------------------------------------------------------------


def trade(side: TradeSide, trader: str, lamports: int) -> TradeObserved:
    return TradeObserved(
        sig(1), mint(1), trader, side, "pump", 1, lamports, None, None, None, None, None
    )


def test_trade_flow_features() -> None:
    a, b, c = mint(2), mint(3), mint(4)
    trades = [
        (NOW - timedelta(seconds=250), trade(TradeSide.BUY, a, 10**9)),
        (NOW - timedelta(seconds=200), trade(TradeSide.BUY, b, 10**9)),
        (NOW - timedelta(seconds=60), trade(TradeSide.BUY, c, 10**9)),
        (NOW - timedelta(seconds=50), trade(TradeSide.BUY, c, 10**9)),
        (NOW - timedelta(seconds=30), trade(TradeSide.SELL, a, 10**9)),
    ]
    flow = compute_trade_flow(trades, NOW, timedelta(seconds=120), timedelta(seconds=180))
    assert (flow.n_buys, flow.n_sells, flow.unique_buyers, flow.unique_sellers) == (4, 1, 3, 1)
    assert flow.buy_ratio == Decimal("0.8")
    assert flow.acceleration == Decimal("1.5")  # 2 buys/2 min vs 2 buys/3 min
    assert flow.wash_ratio == Decimal("0.3333")
    assert flow.sellers == {a}


def test_trade_flow_without_history() -> None:
    flow = compute_trade_flow([], NOW, timedelta(seconds=120), timedelta(seconds=180))
    assert flow.buy_ratio is None and flow.acceleration is None and flow.wash_ratio is None


# --- scoring -----------------------------------------------------------------------------


def test_ramp() -> None:
    assert ramp(Decimal("0.03"), Decimal("0.05"), Decimal("0.01")) == Decimal("50.00")
    assert ramp(10, 5, 40) == Decimal("14.29")
    assert ramp(100, 5, 40) == Decimal("100.00")
    assert ramp(None, 0, 1) is None


def perfect_inputs(**overrides: object) -> ScoreInputs:
    values: dict[str, object] = {
        "entry_price_impact": Decimal("0.005"),
        "buy_ratio": Decimal("0.9"),
        "acceleration": Decimal("3"),
        "top10_share": Decimal("0.1"),
        "creator_share": Decimal("0"),
        "unique_buyers": 50,
        "wash_ratio": Decimal("0.05"),
        "fresh_holder_ratio": Decimal("0"),
        "largest_funding_cluster": 1,
        "creation_slot_txs": 0,
    } | overrides
    return ScoreInputs(**values)  # type: ignore[arg-type]


def test_perfect_token_scores_100_without_sentiment() -> None:
    result = score(perfect_inputs(), CFG)
    assert result.total == Decimal("100.00") and result.band == "priority"
    assert result.components["sentiment"] is None


def test_missing_component_counts_as_zero() -> None:
    result = score(perfect_inputs(fresh_holder_ratio=None), CFG)
    assert result.total == Decimal("77.78")  # 70 / 90 of the weight
    assert result.missing == ["wallet"]


def test_prescore_excludes_wallet_component() -> None:
    result = score(perfect_inputs(fresh_holder_ratio=None), CFG, exclude=("wallet",))
    assert result.total == Decimal("100.00")


def test_sentiment_counts_when_present() -> None:
    result = score(perfect_inputs(sentiment=Decimal(0)), CFG)
    assert result.total == Decimal("90.00")


def test_bands() -> None:
    assert [band_for(Decimal(v), CFG) for v in (59, 60, 75, 85)] == [
        "reject",
        "watch",
        "candidate",
        "priority",
    ]
