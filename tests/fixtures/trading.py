"""Helpers for paper-trading tests: curve states, market messages, an in-memory store."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from backend.database.enums import PositionStatus
from backend.integrations.jupiter import JupiterQuote
from backend.scanner.messages import decode_json
from backend.scanner.providers.base import RawMessage
from backend.trading.config import PaperTradingConfig
from backend.trading.positions import PaperPosition
from backend.trading.repository import RestoredStrategy, TradeRecord
from backend.trading.strategies import AnalysisSignal
from tests.fixtures.pumpportal import b58

K = 30 * 10**9 * 1_073_000_000 * 10**6  # constant product of the initial pump.fun curve
BASE_SOL = 45 * 10**9  # a curve that already received ~15 SOL


def reserves(price_multiple: Decimal | float = 1) -> tuple[int, int]:
    """Curve reserves whose spot price is ``price_multiple`` x the BASE_SOL state."""
    v_sol = int(Decimal(BASE_SOL) * Decimal(str(price_multiple)).sqrt())
    return v_sol, K // v_sol


def _ui(v_sol: int, v_tokens: int) -> dict[str, str]:
    return {
        "vSolInBondingCurve": str(Decimal(v_sol) / 10**9),
        "vTokensInBondingCurve": str(Decimal(v_tokens) / 10**6),
    }


def created(mint: str, at: datetime, creator: str, multiple: float = 1) -> RawMessage:
    payload = {
        "signature": b58(88, hash((mint, "c")) % 10**6),
        "mint": mint,
        "txType": "create",
        "traderPublicKey": creator,
        "pool": "pump",
        **_ui(*reserves(multiple)),
    }
    return RawMessage("pumpportal", at, decode_json(json.dumps(payload)), 100)


def traded(
    mint: str,
    at: datetime,
    multiple: float,
    *,
    side: str = "buy",
    trader: str | None = None,
    sol: float = 0.5,
    seq: int = 0,
) -> RawMessage:
    payload = {
        "signature": b58(88, hash((mint, at.isoformat(), seq)) % 10**9),
        "mint": mint,
        "txType": side,
        "pool": "pump",
        "traderPublicKey": trader or b58(44, seq + 1000),
        "solAmount": str(sol),
        "tokenAmount": "1000",
        **_ui(*reserves(multiple)),
    }
    return RawMessage("pumpportal", at, decode_json(json.dumps(payload)), 100)


def migrated(mint: str, at: datetime) -> RawMessage:
    payload = {"signature": b58(88, hash((mint, "m")) % 10**6), "mint": mint, "txType": "migrate"}
    return RawMessage("pumpportal", at, decode_json(json.dumps(payload)), 100)


def a_signal(mint: str, at: datetime, **overrides: Any) -> AnalysisSignal:
    values: dict[str, Any] = {
        "token_id": 1,
        "mint": mint,
        "at": at,
        "decision": "candidate",
        "critical_flags": (),
        "total_score": Decimal(80),
        "metrics": {
            "curve_tokens_sold_share": "0.40",
            "unique_buyers": 55,
            "entry_price_impact": "0.02",
        },
    } | overrides
    return AnalysisSignal(**values)


@dataclass
class MemoryStore:
    """In-memory TradingStore recording everything for assertions."""

    positions: dict[int, dict[str, Any]] = field(default_factory=dict)
    trades: list[tuple[int, TradeRecord]] = field(default_factory=list)
    decisions: list[tuple[str, str, str, str]] = field(default_factory=list)
    snapshots: list[tuple[str, datetime, dict[str, Any]]] = field(default_factory=list)
    restored: dict[str, RestoredStrategy] = field(default_factory=dict)

    async def config_version_id(self, config: PaperTradingConfig) -> int:
        return 1

    async def open_position(self, pos: PaperPosition, config_id: int, run_id: str) -> int:
        new_id = len(self.positions) + 1
        self.positions[new_id] = {
            "strategy": pos.strategy,
            "mint": pos.mint,
            "status": "open",
            "cost": pos.cost_lamports,
            "quantity": pos.quantity,
        }
        return new_id

    async def record_trade(self, position_id: int, trade: TradeRecord) -> None:
        self.trades.append((position_id, trade))

    async def update_position(self, pos: PaperPosition) -> None:
        if pos.db_id is not None:
            self.positions[pos.db_id] |= {"remaining": pos.remaining, "cost": pos.cost_lamports}

    async def close_position(
        self, pos: PaperPosition, pnl: int | None, at: datetime, status: PositionStatus
    ) -> None:
        if pos.db_id is not None:
            self.positions[pos.db_id] |= {
                "status": "closed",
                "pnl": pnl,
                "reason": pos.exit_reason,
                "closed_at": at,
                "cost": pos.cost_lamports,
            }

    async def record_decision(
        self,
        agent: str,
        mint: str,
        decision: str,
        reason: str,
        payload: dict[str, Any],
        at: datetime,
        config_id: int,
    ) -> None:
        self.decisions.append((agent, mint, decision, reason))

    async def snapshot(
        self, run_id: str, strategy: str, at: datetime, values: dict[str, Any]
    ) -> None:
        self.snapshots.append((strategy, at, values))

    async def restore(self, run_id: str, strategy: str, since: datetime) -> RestoredStrategy:
        return self.restored.get(strategy, RestoredStrategy([], [], 0, 0))


class FakeJupiter:
    """Quote source returning a fixed fraction of a reference rate (lamports <-> raw tokens)."""

    def __init__(self, tokens_per_lamport: Decimal | None) -> None:
        self.rate = tokens_per_lamport
        self.calls = 0

    async def buy_quote(self, mint: str, lamports: int) -> JupiterQuote | None:
        self.calls += 1
        if self.rate is None:
            return None
        return JupiterQuote(lamports, int(Decimal(lamports) * self.rate), 50, None)

    async def sell_quote(self, mint: str, raw_tokens: int) -> JupiterQuote | None:
        self.calls += 1
        if self.rate is None:
            return None
        return JupiterQuote(raw_tokens, int(Decimal(raw_tokens) / self.rate), 50, None)


def seconds(t0: datetime, s: float) -> datetime:
    return t0 + timedelta(seconds=s)
