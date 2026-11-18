"""Exact integer math on a constant-product bonding curve (virtual reserves).

Used to estimate what a trade of a given size would really cost: price impact on entry and the
full round trip (buy then immediately sell the same tokens), protocol fees included. Network
fees, priority fees and rent are not included here (see the execution cost model in M4).
"""

from dataclasses import dataclass
from decimal import Decimal

BPS = 10_000


def buy_tokens_out(v_sol: int, v_tokens: int, sol_in: int, fee_bps: int) -> int:
    if min(v_sol, v_tokens, sol_in) <= 0:
        return 0
    sol_net = sol_in * (BPS - fee_bps) // BPS
    return v_tokens * sol_net // (v_sol + sol_net)


def sell_sol_out(v_sol: int, v_tokens: int, tokens_in: int, fee_bps: int) -> int:
    if min(v_sol, v_tokens, tokens_in) <= 0:
        return 0
    gross = v_sol * tokens_in // (v_tokens + tokens_in)
    return gross * (BPS - fee_bps) // BPS


@dataclass(frozen=True, slots=True)
class TradeCostEstimate:
    sol_in: int
    tokens_out: int
    sol_back: int
    entry_price_impact: Decimal  # effective entry price / spot price - 1 (fees included)
    round_trip_cost: Decimal  # 1 - sol_back / sol_in: equals the fees on a pure curve


def estimate_round_trip(
    v_sol: int, v_tokens: int, sol_in: int, fee_bps: int
) -> TradeCostEstimate | None:
    tokens = buy_tokens_out(v_sol, v_tokens, sol_in, fee_bps)
    if tokens <= 0:
        return None
    sol_after_buy = v_sol + sol_in * (BPS - fee_bps) // BPS
    tokens_after_buy = v_tokens - tokens
    back = sell_sol_out(sol_after_buy, tokens_after_buy, tokens, fee_bps)
    spot = Decimal(v_sol) / Decimal(v_tokens)
    effective = Decimal(sol_in) / Decimal(tokens)
    q = Decimal("0.000001")
    return TradeCostEstimate(
        sol_in=sol_in,
        tokens_out=tokens,
        sol_back=back,
        entry_price_impact=(effective / spot - 1).quantize(q),
        round_trip_cost=(1 - Decimal(back) / Decimal(sol_in)).quantize(q),
    )
