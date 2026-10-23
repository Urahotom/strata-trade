"""Buy&Holdベンチマーク（benchmark.py）と年別セクションのテスト."""

from __future__ import annotations

import pytest

from strata.backtest import (
    BenchmarkResult,
    buy_hold,
    group_stats,
    key_exit_year,
    render_markdown,
)
from strata.backtest.trades import Fill, build_trades
from strata.data.base import DailyBar


def bar(symbol: str, day: str, close: float) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        source="manual",
    )


def series(symbol: str, pairs: list[tuple[str, float]]) -> list[DailyBar]:
    return [bar(symbol, d, c) for d, c in pairs]


class TestBuyHold:
    def test_倍になればリターン100パーセント(self) -> None:
        bars = {
            "8306": series(
                "8306", [("2023-01-04", 100.0), ("2023-06-01", 150.0), ("2023-12-01", 200.0)]
            )
        }
        result = buy_hold(bars, 1_000_000)
        assert result.total_return == 1.0
        assert result.curve[-1].equity == 2_000_000

    def test_中間の下落が最大DD(self) -> None:
        bars = {
            "A": series("A", [("2023-01-04", 100.0), ("2023-05-01", 50.0), ("2023-12-01", 100.0)])
        }
        result = buy_hold(bars, 100.0)
        assert result.max_drawdown == 0.5
        assert result.total_return == 0.0

    def test_開始日の異なる2銘柄は未開始枠が現金(self) -> None:
        bars = {
            "A": series("A", [("2023-01-04", 100.0), ("2023-02-01", 100.0)]),
            "B": series("B", [("2023-02-01", 100.0)]),
        }
        result = buy_hold(bars, 200.0)
        first = result.curve[0]
        assert first.equity == 200.0  # B枠は現金で総額不変
        assert first.cash == 100.0
        assert first.market_value == 100.0

    def test_年次リターン(self) -> None:
        bars = {
            "A": series(
                "A",
                [
                    ("2022-06-01", 100.0),
                    ("2022-12-28", 110.0),
                    ("2023-06-01", 115.0),
                    ("2023-12-27", 121.0),
                ],
            )
        }
        result = buy_hold(bars, 100.0)
        assert result.yearly_returns["2022"] == pytest.approx(0.10)
        assert result.yearly_returns["2023"] == pytest.approx(0.10)

    def test_空データはゼロ(self) -> None:
        assert buy_hold({}, 1000.0) == BenchmarkResult()


class TestYearSection:
    def _trades(self):  # type: ignore[no-untyped-def]
        return build_trades(
            [
                Fill(
                    traded_at="2022-03-01T15:30:00",
                    date="2022-03-01",
                    symbol="8306",
                    side="BUY",
                    qty=100,
                    price=1000.0,
                    source="ma_cross",
                    reason="gc",
                ),
                Fill(
                    traded_at="2022-05-01T15:30:00",
                    date="2022-05-01",
                    symbol="8306",
                    side="SELL",
                    qty=100,
                    price=1100.0,
                    source="ma_cross",
                    reason="dc",
                ),
                Fill(
                    traded_at="2023-03-01T15:30:00",
                    date="2023-03-01",
                    symbol="8306",
                    side="BUY",
                    qty=100,
                    price=1100.0,
                    source="ma_cross",
                    reason="gc",
                ),
                Fill(
                    traded_at="2023-04-01T15:30:00",
                    date="2023-04-01",
                    symbol="8306",
                    side="SELL",
                    qty=100,
                    price=1000.0,
                    source="ma_cross",
                    reason="dc",
                ),
            ]
        )[0]

    def test_年別グループとベンチマーク対照(self) -> None:
        trades = self._trades()
        by_year = group_stats(trades, key_exit_year)
        assert set(by_year) == {"2022", "2023"}
        assert by_year["2022"].total_pnl == 10_000
        assert by_year["2023"].total_pnl == -10_000

        benchmark = buy_hold(
            {"8306": series("8306", [("2022-01-04", 1000.0), ("2023-12-01", 1200.0)])},
            100_000,
        )
        md = render_markdown(
            strategy_name="demo",
            metrics={"total_return": 0.0},
            trades=trades,
            by_year=by_year,
            benchmark=benchmark,
        )
        assert "## 年別（戦略 vs Buy&Hold）" in md
        assert "ベンチマーク Buy&Hold" in md
        assert "2022" in md and "2023" in md
