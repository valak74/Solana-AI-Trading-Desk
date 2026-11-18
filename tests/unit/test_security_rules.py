from decimal import Decimal

import pytest

from backend.blockchain.accounts import BondingCurveState, parse_mint
from backend.blockchain.curve_math import estimate_round_trip
from backend.database.enums import RiskLevel
from backend.security import rules
from backend.security.config import SecurityThresholds
from backend.security.findings import Findings
from tests.fixtures.chain import mint_account
from tests.fixtures.pumpportal import mint

T = SecurityThresholds()


def mint_findings(**kwargs: object) -> Findings:
    f = Findings()
    rules.check_mint(parse_mint(mint_account(**kwargs)), T, f)  # type: ignore[arg-type]
    return f


def ext(name: str, state: dict[str, object] | None = None) -> dict[str, object]:
    return {"extension": name, "state": state or {}}


def test_clean_mint_has_no_findings() -> None:
    f = mint_findings()
    assert f.critical == [] and f.warnings == []
    assert f.security_score == 100 and f.risk_level is RiskLevel.LOW


def test_active_authorities_are_critical() -> None:
    f = mint_findings(mint_authority=mint(1), freeze_authority=mint(2))
    assert f.critical == ["mint_authority_active", "freeze_authority_active"]
    assert f.security_score == 0 and f.risk_level is RiskLevel.CRITICAL


@pytest.mark.parametrize(
    ("extension", "critical", "warning"),
    [
        (ext("metadataPointer"), [], []),
        (ext("tokenMetadata"), [], []),
        (ext("mintCloseAuthority"), [], ["token2022_mintCloseAuthority"]),
        (ext("permanentDelegate", {"delegate": None}), [], []),
        (ext("permanentDelegate", {"delegate": mint(3)}), ["token2022_permanent_delegate"], []),
        (ext("transferHook", {"programId": None}), [], []),
        (ext("transferHook", {"programId": mint(4)}), ["token2022_transfer_hook"], []),
        (
            ext("defaultAccountState", {"accountState": "frozen"}),
            ["token2022_default_account_frozen"],
            [],
        ),
        (ext("defaultAccountState", {"accountState": "initialized"}), [], []),
        (
            ext("pausableConfig", {"authority": mint(5), "paused": False}),
            ["token2022_pausable"],
            [],
        ),
        (ext("nonTransferable"), ["token2022_non_transferable"], []),
        (ext("somethingNew"), ["token2022_unknown_extension:somethingNew"], []),
        (
            ext("transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 50}}),
            [],
            ["token2022_transfer_fee"],
        ),
        (
            ext("transferFeeConfig", {"newerTransferFee": {"transferFeeBasisPoints": 500}}),
            ["token2022_transfer_fee_high"],
            [],
        ),
        (ext("transferFeeConfig", {}), ["token2022_transfer_fee_high"], []),  # unreadable
    ],
)
def test_token_2022_extensions(
    extension: dict[str, object], critical: list[str], warning: list[str]
) -> None:
    f = mint_findings(token_2022=True, extensions=[extension])
    assert f.critical == critical
    assert f.warnings == warning


def test_holder_concentration() -> None:
    f = Findings()
    shares = {mint(1): Decimal("0.25"), mint(2): Decimal("0.10"), mint(3): Decimal("0.20")}
    rules.check_holders(shares, mint(3), T, f)
    assert set(f.critical) == {
        "top_holder_concentration",
        "top10_concentration",
        "creator_holds_large_share",
    }
    ok = Findings()
    rules.check_holders({mint(1): Decimal("0.04"), mint(2): Decimal("0.03")}, None, T, ok)
    assert ok.critical == [] and ok.warnings == []
    assert ok.metrics["top10_share"] == "0.07"


def test_curve_complete_and_costs() -> None:
    curve = BondingCurveState(900 * 10**12, 40 * 10**9, 620 * 10**12, 10 * 10**9, 10**15, True)
    f = Findings()
    rules.check_curve(
        curve,
        estimate_round_trip(
            curve.virtual_sol_reserves, curve.virtual_token_reserves, 5 * 10**9, 125
        ),
        T,
        f,
    )
    assert "curve_complete_not_tradable" in f.critical
    assert f.warnings == ["high_entry_price_impact"]
    missing = Findings()
    rules.check_curve(curve, None, T, missing)
    assert missing.missing == ["trade_cost_estimate"]


def test_creator_rules() -> None:
    f = Findings()
    rules.check_creator_sold(mint(1), {mint(1), mint(2)}, f)
    rules.check_creator_history(3, T, f)
    assert f.critical == ["creator_sold"] and f.warnings == ["serial_creator"]
    g = Findings()
    rules.check_creator_history(10, T, g)
    assert g.critical == ["serial_creator"]


def test_bundle_and_missing_data() -> None:
    f = Findings()
    rules.check_bundle(None, T, f)
    assert f.missing == ["creation_slot_activity"] and f.risk_level is RiskLevel.HIGH
    w, c = Findings(), Findings()
    rules.check_bundle(4, T, w)
    rules.check_bundle(8, T, c)
    assert w.warnings == ["bundled_launch"] and c.critical == ["bundled_launch"]


def test_funding_clusters_and_fresh_wallets() -> None:
    funder, creator = mint(9), mint(8)
    f = Findings()
    holders = {mint(1): funder, mint(2): funder, mint(3): funder, mint(4): None}
    rules.check_funding(holders, {m: True for m in holders}, None, T, f)
    assert f.critical == ["coordinated_wallets"]
    assert f.warnings == ["mostly_fresh_holders"]
    g = Findings()
    rules.check_funding({mint(1): creator}, {mint(1): False}, creator, T, g)
    assert g.critical == ["creator_funded_holders"]


def test_rugcheck_is_a_warning_by_default() -> None:
    f = Findings()
    rules.check_rugcheck(["Freeze Authority still enabled"], T, f)
    assert f.warnings == ["rugcheck:Freeze Authority still enabled"]
    strict = Findings()
    rules.check_rugcheck(["x"], T.model_copy(update={"rugcheck_danger_is_critical": True}), strict)
    assert strict.critical == ["rugcheck:x"]


def test_findings_are_never_duplicated_and_score_degrades() -> None:
    f = Findings()
    for _ in range(2):
        f.add_warning("a")
    f.add_warning("b")
    assert f.warnings == ["a", "b"] and f.security_score == 70
    f.add_warning("c")
    assert f.risk_level is RiskLevel.HIGH
