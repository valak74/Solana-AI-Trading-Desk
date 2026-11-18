"""Paper execution: what a real order would most plausibly have obtained, never better.

Fill price = the WORST of the executable quotes available at execution time (decision time +
latency): exact curve math on the latest recorded reserves, and a Jupiter quote when a client is
configured. Then: aggregator fee on curve fills (live orders would go through Jupiter), an extra
slippage haircut, network fees (charged even when the transaction fails, as on Solana) and the
token-account rent (paid on entry, refunded only when the position is fully closed).
"""

import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from backend.blockchain.curve_math import BPS, buy_tokens_out, sell_sol_out
from backend.integrations.jupiter import JupiterQuote
from backend.trading.config import ExecutionModel
from backend.trading.market import TokenMarket


class QuoteSource(Protocol):
    async def buy_quote(self, mint: str, lamports: int) -> JupiterQuote | None: ...

    async def sell_quote(self, mint: str, raw_tokens: int) -> JupiterQuote | None: ...


@dataclass(frozen=True, slots=True)
class Fill:
    ok: bool
    side: str
    in_amount: int  # lamports for a buy, raw tokens for a sell
    out_amount: int  # raw tokens for a buy, lamports for a sell (after fees and haircut)
    network_fee_lamports: int
    fees_lamports: int  # protocol + aggregator fees, estimated in lamports
    fill_model: str
    quotes: dict[str, int] = field(default_factory=dict)
    failure_reason: str | None = None


def _haircut(amount: int, bps: int) -> int:
    return amount * (BPS - bps) // BPS


class PaperExecutor:
    def __init__(
        self,
        model: ExecutionModel,
        rng: random.Random,
        jupiter: QuoteSource | None = None,
    ) -> None:
        self._m = model
        self._rng = rng
        self._jupiter = jupiter

    @property
    def has_aggregator(self) -> bool:
        return self._jupiter is not None

    def _failed(self, side: str, amount: int, reason: str, fee: int) -> Fill:
        return Fill(False, side, amount, 0, fee, 0, "none", failure_reason=reason)

    async def buy(self, market: TokenMarket, lamports: int) -> Fill:
        m = self._m
        quotes: dict[str, int] = {}
        if not market.migrated and market.v_sol and market.v_tokens:
            curve_out = buy_tokens_out(market.v_sol, market.v_tokens, lamports, m.curve_fee_bps)
            quotes["curve"] = _haircut(curve_out, m.aggregator_fee_bps)
        if self._jupiter is not None:
            quote = await self._jupiter.buy_quote(market.mint, lamports)
            if quote is not None:
                quotes["jupiter"] = quote.out_amount
        if not quotes:
            return self._failed("buy", lamports, "no_executable_quote", 0)
        if self._rng.random() < float(m.buy_failure_rate):
            return self._failed("buy", lamports, "simulated_failure", m.network_fee_lamports)
        model, worst = min(quotes.items(), key=lambda kv: kv[1])
        tokens = _haircut(worst, m.extra_slippage_bps)
        if tokens <= 0:
            return self._failed("buy", lamports, "zero_output", m.network_fee_lamports)
        fees = lamports * (m.curve_fee_bps + m.aggregator_fee_bps) // BPS
        return Fill(
            True,
            "buy",
            lamports,
            tokens,
            m.network_fee_lamports,
            fees,
            "worst_of" if len(quotes) > 1 else model,
            quotes,
        )

    async def sell(self, market: TokenMarket, raw_tokens: int) -> Fill:
        m = self._m
        quotes: dict[str, int] = {}
        if not market.migrated and market.v_sol and market.v_tokens:
            curve_out = sell_sol_out(market.v_sol, market.v_tokens, raw_tokens, m.curve_fee_bps)
            quotes["curve"] = _haircut(curve_out, m.aggregator_fee_bps)
        if self._jupiter is not None:
            quote = await self._jupiter.sell_quote(market.mint, raw_tokens)
            if quote is not None:
                quotes["jupiter"] = quote.out_amount
        if not quotes:
            return self._failed("sell", raw_tokens, "no_executable_quote", 0)
        if self._rng.random() < float(m.sell_failure_rate):
            return self._failed("sell", raw_tokens, "simulated_failure", m.network_fee_lamports)
        model, worst = min(quotes.items(), key=lambda kv: kv[1])
        lamports = _haircut(worst, m.extra_slippage_bps)
        gross = lamports * BPS // max(1, BPS - m.curve_fee_bps - m.aggregator_fee_bps)
        return Fill(
            True,
            "sell",
            raw_tokens,
            lamports,
            m.network_fee_lamports,
            max(0, gross - lamports),
            "worst_of" if len(quotes) > 1 else model,
            quotes,
        )

    def expected_buy(self, market: TokenMarket, lamports: int) -> int | None:
        """Tokens expected at decision time (curve, no haircut): reference for slippage."""
        if market.migrated or not market.v_sol or not market.v_tokens:
            return None
        out = buy_tokens_out(market.v_sol, market.v_tokens, lamports, self._m.curve_fee_bps)
        return _haircut(out, self._m.aggregator_fee_bps)

    def expected_sell(self, market: TokenMarket, raw_tokens: int) -> int | None:
        if market.migrated or not market.v_sol or not market.v_tokens:
            return None
        out = sell_sol_out(market.v_sol, market.v_tokens, raw_tokens, self._m.curve_fee_bps)
        return _haircut(out, self._m.aggregator_fee_bps)

    async def aggregator_value(self, mint: str, raw_tokens: int) -> int | None:
        """Mark-to-market from an aggregator quote (graduated tokens), all costs included."""
        if self._jupiter is None or raw_tokens <= 0:
            return None
        quote = await self._jupiter.sell_quote(mint, raw_tokens)
        if quote is None:
            return None
        net = _haircut(quote.out_amount, self._m.extra_slippage_bps)
        return max(0, net - self._m.network_fee_lamports)

    async def aggregator_buy_impact(self, mint: str, lamports: int) -> Decimal | None:
        """Price impact of ``lamports`` versus a 20x smaller order (two aggregator quotes)."""
        if self._jupiter is None or lamports < 20:
            return None
        small = lamports // 20
        small_q = await self._jupiter.buy_quote(mint, small)
        full_q = await self._jupiter.buy_quote(mint, lamports)
        if small_q is None or full_q is None or small_q.out_amount <= 0:
            return None
        rate_small = Decimal(small_q.out_amount) / Decimal(small)
        rate_full = Decimal(full_q.out_amount) / Decimal(lamports)
        return (1 - rate_full / rate_small).quantize(Decimal("0.000001"))

    def value_of(self, market: TokenMarket, raw_tokens: int) -> int | None:
        """Mark-to-market: lamports a sale would bring right now (curve only, all costs)."""
        m = self._m
        if market.migrated or not market.v_sol or not market.v_tokens or raw_tokens <= 0:
            return None
        out = sell_sol_out(market.v_sol, market.v_tokens, raw_tokens, m.curve_fee_bps)
        net = _haircut(_haircut(out, m.aggregator_fee_bps), m.extra_slippage_bps)
        return max(0, net - m.network_fee_lamports)


def effective_price(lamports: int, raw_tokens: int, decimals: int = 6) -> Decimal | None:
    """SOL per whole token actually paid or received."""
    if lamports <= 0 or raw_tokens <= 0:
        return None
    sol = Decimal(lamports) / Decimal(10**9)
    return (sol / (Decimal(raw_tokens) / Decimal(10**decimals))).quantize(Decimal("1e-18"))
