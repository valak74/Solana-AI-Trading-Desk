"""pump.fun bonding-curve constants and derived metrics.

The curve parameters below are the long-standing pump.fun defaults (30 SOL virtual SOL reserve,
1,073,000,000 virtual tokens of which 793,100,000 are sold on the curve). pump.fun has changed
fees and launch mechanics over time, so derived values are labelled *estimates*: step M3
verifies them against on-chain account data before any decision relies on them.
"""

from decimal import ROUND_HALF_EVEN, Decimal

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

INITIAL_VIRTUAL_SOL_LAMPORTS = 30 * 10**9
INITIAL_VIRTUAL_TOKENS_RAW = 1_073_000_000 * 10**6
CURVE_SALE_TOKENS_RAW = 793_100_000 * 10**6


def estimated_curve_progress(v_tokens_raw: int | None) -> Decimal | None:
    """Fraction of the curve's sale supply already sold (0..1)."""
    if v_tokens_raw is None or v_tokens_raw <= 0:
        return None
    sold = INITIAL_VIRTUAL_TOKENS_RAW - v_tokens_raw
    progress = Decimal(sold) / Decimal(CURVE_SALE_TOKENS_RAW)
    progress = min(Decimal(1), max(Decimal(0), progress))
    return progress.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


def estimated_real_sol_lamports(v_sol_lamports: int | None) -> int | None:
    """SOL actually deposited in the curve (virtual reserve minus the initial virtual SOL)."""
    if v_sol_lamports is None or v_sol_lamports <= 0:
        return None
    return max(0, v_sol_lamports - INITIAL_VIRTUAL_SOL_LAMPORTS)
