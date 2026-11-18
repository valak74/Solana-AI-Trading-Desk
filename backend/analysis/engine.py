"""Token analysis: cheap checks first, deep checks only for promising tokens.

Stage 1 (about 4 credits): mint account, bonding curve, largest holders, recorded trade flow.
Stage 2 (about 10-15 credits, only without critical flag and with pre-score above the
threshold): launch-slot activity, funding of the top holders, optional RugCheck opinion.

A token that did not get stage 2 can never be classified above "watch".
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from backend.analysis.flows import TradeFlow
from backend.blockchain.accounts import AccountDecodeError, parse_bonding_curve, parse_mint
from backend.blockchain.curve_math import estimate_round_trip
from backend.integrations.helius import RpcError
from backend.integrations.rugcheck import RugCheckClient
from backend.scoring.scorer import ScoreBreakdown, ScoreInputs, score
from backend.security import rules
from backend.security.config import AnalysisConfig
from backend.security.findings import Findings
from backend.security.onchain import (
    SolanaReader,
    creation_slot_activity,
    holder_shares,
    profile_wallet,
)

# Expected failures of one data source. BudgetExceeded is deliberately not caught: running out
# of credits must stop the analysis instead of producing a partial, misleading result.
SOURCE_ERRORS = (RpcError, httpx.HTTPError, AccountDecodeError, ValueError, KeyError, TypeError)
BAND_ORDER = ("reject", "watch", "candidate", "priority")


@dataclass(frozen=True, slots=True)
class AnalysisTarget:
    token_id: int
    mint: str
    creator: str | None
    curve_address: str
    first_seen_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    findings: Findings
    prescore: ScoreBreakdown
    final: ScoreBreakdown
    deep_done: bool
    analyzed_at: datetime

    @property
    def decision(self) -> str:
        if self.findings.critical:
            return "reject"
        band = self.final.band
        if not self.deep_done and BAND_ORDER.index(band) > BAND_ORDER.index("watch"):
            return "watch"
        return band


class TokenAnalyzer:
    def __init__(
        self,
        rpc: SolanaReader,
        config: AnalysisConfig,
        rugcheck: RugCheckClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._rpc = rpc
        self._cfg = config
        self._rugcheck = rugcheck
        self._clock = clock

    async def analyze(
        self, target: AnalysisTarget, flow: TradeFlow, creator_launches_24h: int
    ) -> AnalysisResult:
        now = self._clock()
        cfg, t = self._cfg, self._cfg.security
        f = Findings()
        f.metrics["token_age_seconds"] = int((now - target.first_seen_at).total_seconds())
        f.metrics |= flow.as_metrics()
        inputs: dict[str, object] = {
            "buy_ratio": flow.buy_ratio,
            "acceleration": flow.acceleration,
            "unique_buyers": flow.unique_buyers,
            "wash_ratio": flow.wash_ratio,
        }

        supply = await self._check_mint(target, f)
        inputs["entry_price_impact"] = await self._check_curve(target, f)
        shares = await self._check_holders(target, supply, f)
        if shares is not None:
            inputs["top10_share"] = Decimal(str(f.metrics["top10_share"]))
            inputs["creator_share"] = Decimal(str(f.metrics["creator_share"]))
        rules.check_creator_history(creator_launches_24h, t, f)
        rules.check_creator_sold(target.creator, set(flow.sellers), f)

        prescore = score(ScoreInputs(**inputs), cfg, exclude=("wallet",))  # type: ignore[arg-type]
        deep = not f.critical and prescore.total >= cfg.deep_analysis_min_prescore
        if deep:
            inputs |= await self._deep_checks(target, shares or {}, now, f)
        f.metrics["deep_analysis"] = deep
        final = score(ScoreInputs(**inputs), cfg)  # type: ignore[arg-type]
        return AnalysisResult(f, prescore, final, deep, now)

    async def _check_mint(self, target: AnalysisTarget, f: Findings) -> int | None:
        try:
            mint = parse_mint(await self._rpc.get_account_info(target.mint))
        except SOURCE_ERRORS:
            f.add_missing("mint_account")
            f.add_critical("mint_unverifiable")
            return None
        rules.check_mint(mint, self._cfg.security, f)
        f.metrics["supply_raw"] = mint.supply_raw
        return mint.supply_raw

    async def _check_curve(self, target: AnalysisTarget, f: Findings) -> Decimal | None:
        try:
            curve = parse_bonding_curve(
                await self._rpc.get_account_info(target.curve_address, "base64")
            )
        except SOURCE_ERRORS:
            f.add_missing("bonding_curve")
            f.add_critical("curve_unverifiable")
            return None
        cost = estimate_round_trip(
            curve.virtual_sol_reserves,
            curve.virtual_token_reserves,
            self._cfg.trade_size_lamports,
            self._cfg.curve_fee_bps,
        )
        rules.check_curve(curve, cost, self._cfg.security, f)
        return cost.entry_price_impact if cost else None

    async def _check_holders(
        self, target: AnalysisTarget, supply: int | None, f: Findings
    ) -> dict[str, Decimal] | None:
        if supply is None:
            f.add_missing("holders")
            return None
        try:
            shares = await holder_shares(self._rpc, target.mint, supply, {target.curve_address})
        except SOURCE_ERRORS:
            f.add_missing("holders")
            return None
        rules.check_holders(shares, target.creator, self._cfg.security, f)
        return shares

    async def _deep_checks(
        self, target: AnalysisTarget, shares: dict[str, Decimal], now: datetime, f: Findings
    ) -> dict[str, object]:
        cfg, t = self._cfg, self._cfg.security
        extra: dict[str, object] = {}
        try:
            slot_txs = await creation_slot_activity(
                self._rpc, target.curve_address, cfg.bundle_max_pages
            )
        except SOURCE_ERRORS:
            slot_txs = None
        rules.check_bundle(slot_txs, t, f)
        extra["creation_slot_txs"] = slot_txs

        top = sorted(shares, key=lambda owner: shares[owner], reverse=True)
        funders: dict[str, str | None] = {}
        fresh: dict[str, bool] = {}
        for owner in top[: cfg.holders_to_profile]:
            try:
                profile = await profile_wallet(
                    self._rpc,
                    owner,
                    t.fresh_wallet_max_txs,
                    timedelta(hours=t.fresh_wallet_max_age_hours),
                    now,
                )
            except SOURCE_ERRORS:
                f.add_missing("holder_profiles")
                continue
            funders[owner], fresh[owner] = profile.funder, profile.fresh
        rules.check_funding(funders, fresh, target.creator, t, f)
        if funders:
            extra["fresh_holder_ratio"] = Decimal(sum(fresh.values())) / Decimal(len(fresh))
            extra["largest_funding_cluster"] = f.metrics.get("largest_funding_cluster", 0)
        else:
            extra["fresh_holder_ratio"] = Decimal(0) if not top else None

        if self._rugcheck is not None:
            dangers = await self._rugcheck.dangers(target.mint)
            if dangers is None:
                f.metrics["rugcheck"] = "unavailable"
            else:
                rules.check_rugcheck(dangers, t, f)
        return extra
