"""MDD区間・期待値分解・市場投資時間・期間分割のテスト（P8）."""

from __future__ import annotations

import pytest

from strata.backtest import (
    EquityPoint,
    PeriodRow,
    compute_metrics,
    exposure_stats,
    max_drawdown_span,
    render_markdown,
    render_periods_markdown,
    summarize,
    yearly_exposure,
    yearly_returns,
)
from strata.backtest.metrics import cagr
from strata.backtest.trades import Fill, build_trades


def point(day: str, cash: float, mv: float) -> EquityPoint:
    return EquityPoint(date=day, cash=cash, market_value=mv, equity=cash + mv)


class TestDrawdownSpan:
    def test_ピークから底の日付付き(self) -> None:
        curve = [
            point("2023-01-04", 100.0, 0.0),
            point("2023-02-01", 150.0, 0.0),  # ピーク
            point("2023-03-01", 75.0, 0.0),  # 底（-50%）
            point("2023-04-01", 120.0, 0.0),
        ]
        span = max_drawdown_span(curve)
        assert span is not None
        assert span.depth == 0.5
        assert span.peak_date == "2023-02-01"
        assert span.trough_date == "2023-03-01"

    def test_含み損の日次評価でも出る(self) -> None:
        # 売買決済日はないが保有中の評価額が毀損するケース
        curve = [
            point("2023-01-04", 0.0, 100.0),
            point("2023-02-01", 0.0, 60.0),
            point("2023-03-01", 0.0, 100.0),
        ]
        span = max_drawdown_span(curve)
        assert span is not None
        assert span.depth == 0.4

    def test_空はNone(self) -> None:
        assert max_drawdown_span([]) is None

    def test_回復期間(self) -> None:
        curve = [
            point("2023-01-04", 100.0, 0.0),
            point("2023-02-01", 150.0, 0.0),  # ピーク
            point("2023-03-01", 75.0, 0.0),  # 底
            point("2023-04-01", 120.0, 0.0),
            point("2023-05-01", 160.0, 0.0),  # ピーク水準を回復
        ]
        span = max_drawdown_span(curve)
        assert span is not None
        assert span.recovery_date == "2023-05-01"
        assert span.days_under_water == 89  # 2023-02-01 → 2023-05-01

    def test_未回復なら末尾まで(self) -> None:
        curve = [
            point("2023-01-04", 150.0, 0.0),
            point("2023-03-01", 75.0, 0.0),
            point("2023-04-01", 120.0, 0.0),
        ]
        span = max_drawdown_span(curve)
        assert span is not None
        assert span.recovery_date is None
        assert span.days_under_water == 87  # 2023-01-04 → 2023-04-01


class TestReturns:
    def test_CAGR(self) -> None:
        curve = [point("2023-01-01", 100.0, 0.0), point("2024-01-01", 110.0, 0.0)]
        assert 0.099 < cagr(curve, 100.0) < 0.101

    def test_年率リターンがmetricsに入る(self) -> None:
        curve = [point("2023-01-01", 100.0, 0.0), point("2024-01-01", 110.0, 0.0)]
        metrics = compute_metrics(
            curve,
            initial_cash=100.0,
            executions=0,
            round_trips=0,
            winning_exits=0,
            realized_pnl=0.0,
            rejected=0,
        )
        assert "cagr" in metrics.values

    def test_戦略曲線の年次リターン(self) -> None:
        curve = [
            point("2022-06-01", 110.0, 0.0),
            point("2022-12-28", 110.0, 0.0),
            point("2023-06-01", 121.0, 0.0),
            point("2023-12-27", 121.0, 0.0),
        ]
        yearly = yearly_returns(curve, 100.0)
        assert yearly["2022"] == pytest.approx(0.10)
        assert yearly["2023"] == pytest.approx(0.10)


class TestExposure:
    def test_投資時間率と平均エクスポージャー(self) -> None:
        curve = [
            point("2023-01-04", 100.0, 0.0),  # 現金のみ
            point("2023-02-01", 50.0, 50.0),  # 50%投資
            point("2023-03-01", 0.0, 100.0),  # 100%投資
        ]
        stats = exposure_stats(curve)
        assert stats.days == 3
        assert stats.market_days == 2
        assert stats.time_in_market == 2 / 3
        assert stats.avg_exposure == (0 + 0.5 + 1.0) / 3
        assert stats.max_exposure == 1.0

    def test_年別グループ(self) -> None:
        curve = [
            point("2022-12-01", 50.0, 50.0),
            point("2023-01-04", 100.0, 0.0),
            point("2023-02-01", 0.0, 100.0),
        ]
        yearly = yearly_exposure(curve)
        assert set(yearly) == {"2022", "2023"}
        assert yearly["2022"].time_in_market == 1.0
        assert yearly["2023"].time_in_market == 0.5

    def test_空はゼロ(self) -> None:
        stats = exposure_stats([])
        assert stats.days == 0
        assert stats.time_in_market == 0.0


class TestExpectancy:
    def _trades(self):  # type: ignore[no-untyped-def]
        def fill(day: str, side: str, price: float) -> Fill:
            return Fill(
                traded_at=f"{day}T15:30:00",
                date=day,
                symbol="8306",
                side=side,
                qty=100,
                price=price,
                source="ma_cross",
                reason="x",
            )

        fills = [
            fill("2023-01-04", "BUY", 1000.0),
            fill("2023-01-10", "SELL", 1100.0),  # +10,000
            fill("2023-02-01", "BUY", 1100.0),
            fill("2023-02-05", "SELL", 1050.0),  # -5,000
        ]
        return build_trades(fills)[0]

    def test_平均利益と平均損失(self) -> None:
        stat = summarize(self._trades())
        assert stat.trades == 2
        assert stat.avg_profit == 10_000
        assert stat.avg_loss == 5_000
        # 金額と割合（+10% / -4.545...%）の両方
        assert stat.avg_profit_pct == pytest.approx(0.10)
        assert stat.avg_loss_pct == pytest.approx(50.0 / 1100.0)
        # E = pW-(1-p)L = 0.5*10000 - 0.5*5000 = 2500
        assert stat.expected_value == 2_500

    def test_レポートに期待値分解とMDD区間とエクスポージャー(self) -> None:
        trades = self._trades()
        curve = [point("2023-01-04", 500_000.0, 500_000.0), point("2023-02-05", 505_000.0, 0.0)]
        md = render_markdown(
            strategy_name="demo",
            metrics={"total_return": 0.01, "cagr": 0.005},
            trades=trades,
            curve=curve,
            cost_per_trade=100.0,
        )
        assert "平均利益 10,000 円（+10.00 %）" in md
        assert "平均損失 5,000 円（−4.55 %）" in md
        assert "年率リターン（CAGR）: 0.50 %" in md
        assert "E = p・W − (1−p)・L − C" in md
        assert "2,400 円/回" in md  # 2500 - 100
        assert "## 最大ドローダウン（日次評価額・保有中の含み損込み）" in md
        assert "未回復（水没" in md
        assert "## 市場への投資時間（エクスポージャー）" in md
        assert "| 2023 |" in md

    def test_期間分割テーブル(self) -> None:
        rows = [
            PeriodRow(
                "学習・調整期間", "2015-01-05", "2020-12-30", 0.5, 0.07, 0.2, 30, 0.4, 1000, 0.7
            ),
            PeriodRow(
                "最終評価期間", "2021-01-04", "2026-10-23", -0.1, -0.02, 0.3, 20, 0.35, -50, 0.8
            ),
        ]
        md = render_periods_markdown("ma_cross", rows)
        assert "期間分割検証: ma_cross" in md
        assert "採点対象" in md and "参考・調整用" in md
        assert "-10.00 %" in md

    def test_曲線なしならセクション省略(self) -> None:
        md = render_markdown(strategy_name="demo", metrics={}, trades=self._trades())
        assert "最大ドローダウン" not in md
        assert "市場への投資時間" not in md
