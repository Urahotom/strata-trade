"""Buy&Holdベンチマーク（同一期間・同一資金・均等加重・リバランスなし）.

戦略成績は「絶対値」では語れない。相場が全体の底上げだった局面では、
何もしないリターンと並べて初めて「腕前」が見える（DESIGN §3.9）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from strata.backtest.metrics import EquityPoint, max_drawdown
from strata.data.base import DailyBar


@dataclass(frozen=True)
class BenchmarkResult:
    """同一初期資金で各銘柄に均等投資して持ち続けた場合の成績."""

    curve: list[EquityPoint] = field(default_factory=list)
    total_return: float = 0.0
    max_drawdown: float = 0.0
    #: 年 → その年の期初比リターン（0.1 = +10%）
    yearly_returns: dict[str, float] = field(default_factory=dict)


def buy_hold(bars: Mapping[str, Sequence[DailyBar]], initial_cash: float) -> BenchmarkResult:
    """各銘柄に initial_cash/n を最初終値で買い、期間終了まで保有した想定.

    銘柄ごとにデータ開始日が異なる場合は、開始日までその枠は現金のまま据える。
    """
    series = {symbol: sorted(list(items), key=lambda b: b.date) for symbol, items in bars.items()}
    series = {s: v for s, v in series.items() if v}
    if not series or initial_cash <= 0:
        return BenchmarkResult()

    n = len(series)
    slot = initial_cash / n
    quantities: dict[str, float] = {}
    first_close: dict[str, float] = {}
    for symbol, items in series.items():
        entry = items[0].close
        if entry > 0:
            quantities[symbol] = slot / entry
            first_close[symbol] = entry
    #: 価格不正で参入できなかった枠は期間中ずっと現金
    never_invested_cash = slot * (n - len(quantities))

    closes: dict[tuple[str, str], float] = {}
    for symbol, items in series.items():
        for bar in items:
            closes[(symbol, bar.date)] = bar.close
    dates = sorted({bar.date for items in series.values() for bar in items})

    curve: list[EquityPoint] = []
    for day in dates:
        market_value = 0.0
        held_cash = never_invested_cash
        for symbol, qty in quantities.items():
            price = closes.get((symbol, day))
            if price is None:
                # まだ始まっていない銘柄の枠は現金で据え置き（開始日に自動投資）
                held_cash += qty * first_close[symbol]
                continue
            market_value += qty * price
        equity = held_cash + market_value
        curve.append(
            EquityPoint(date=day, cash=held_cash, market_value=market_value, equity=equity)
        )

    equities = [p.equity for p in curve]
    return BenchmarkResult(
        curve=curve,
        total_return=equities[-1] / initial_cash - 1.0,
        max_drawdown=max_drawdown(equities),
        yearly_returns=_yearly_returns(curve, initial_cash),
    )


def _yearly_returns(curve: Sequence[EquityPoint], initial_cash: float) -> dict[str, float]:
    """年末値の前期比リターン（初年は初期資金比）."""
    last_by_year: dict[str, float] = {}
    for point in curve:
        last_by_year[point.date[:4]] = point.equity
    out: dict[str, float] = {}
    prev_equity = initial_cash
    for year in sorted(last_by_year):
        equity = last_by_year[year]
        if prev_equity > 0:
            out[year] = equity / prev_equity - 1.0
        prev_equity = equity
    return out
