"""バックテスト成績指標（pure関数）."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EquityPoint:
    """営業日終値時点のポートフォリオ状態."""

    date: str
    cash: float
    market_value: float
    equity: float


def max_drawdown(equities: Sequence[float]) -> float:
    """最大ドローダウン（0.0〜1.0の割合。0なら一切毀損なし）."""
    peak = float("-inf")
    mdd = 0.0
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            mdd = max(mdd, (peak - equity) / peak)
    return mdd


@dataclass(frozen=True)
class BacktestMetrics:
    """成績サマリ（キーはメール・レポートにそのまま出せる名前）."""

    values: dict[str, float] = field(default_factory=dict)

    def __getitem__(self, key: str) -> float:
        return self.values[key]


def compute_metrics(
    curve: Sequence[EquityPoint],
    *,
    initial_cash: float,
    executions: int,
    round_trips: int,
    winning_exits: int,
    realized_pnl: float,
    rejected: int,
) -> BacktestMetrics:
    """エクイティ曲線と約定サマリから成績を算出する."""
    values: dict[str, float] = {
        "initial_cash": initial_cash,
        "executions": float(executions),
        "rejected_orders": float(rejected),
        "round_trips": float(round_trips),
        "realized_pnl": realized_pnl,
        "win_rate": (winning_exits / round_trips) if round_trips else 0.0,
    }
    if curve:
        equities = [p.equity for p in curve]
        values["trading_days"] = float(len(curve))
        values["final_equity"] = equities[-1]
        values["total_return"] = equities[-1] / initial_cash - 1.0
        values["max_drawdown"] = max_drawdown(equities)
    return BacktestMetrics(values=values)
