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
class DrawdownSpan:
    """最大ドローダウンの発生区間（ピーク日→底日、depthは 0.0〜1.0）."""

    depth: float
    peak_date: str
    trough_date: str


def max_drawdown_span(curve: Sequence[EquityPoint]) -> DrawdownSpan | None:
    """日次評価額（含み損込み）の曲線から MDD とその区間を返す."""
    peak_equity = float("-inf")
    peak_date = ""
    best: DrawdownSpan | None = None
    for point in curve:
        if point.equity > peak_equity:
            peak_equity = point.equity
            peak_date = point.date
        if peak_equity > 0:
            depth = (peak_equity - point.equity) / peak_equity
            if best is None or depth > best.depth:
                best = DrawdownSpan(depth=depth, peak_date=peak_date, trough_date=point.date)
    return best


@dataclass(frozen=True)
class ExposureStats:
    """市場への投資時間（エクスポージャー）サマリ."""

    #: 対象営業日数
    days: int
    #: 株式を1株でも保有していた日数
    market_days: int
    #: market_days / days（上昇相場で現金を持ちすぎたかの指標）
    time_in_market: float
    #: 日次 market_value / equity の平均
    avg_exposure: float
    max_exposure: float


def exposure_stats(curve: Sequence[EquityPoint]) -> ExposureStats:
    """エクイティ曲線から投資時間率と平均エクスポージャーを算出する."""
    if not curve:
        return ExposureStats(0, 0, 0.0, 0.0, 0.0)
    market_days = sum(1 for p in curve if p.market_value > 0)
    ratios = [p.market_value / p.equity for p in curve if p.equity > 0]
    return ExposureStats(
        days=len(curve),
        market_days=market_days,
        time_in_market=market_days / len(curve),
        avg_exposure=sum(ratios) / len(ratios) if ratios else 0.0,
        max_exposure=max(ratios) if ratios else 0.0,
    )


def yearly_exposure(curve: Sequence[EquityPoint]) -> dict[str, ExposureStats]:
    """年（date の先頭4文字）別の投資時間サマリ."""
    buckets: dict[str, list[EquityPoint]] = {}
    for point in curve:
        buckets.setdefault(point.date[:4], []).append(point)
    return {year: exposure_stats(points) for year, points in sorted(buckets.items())}


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
