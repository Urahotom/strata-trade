"""バックテスト（PaperBroker + Runner）のテスト.

TESTING §3.4: バックテストは運用と同一のリスク・注文コードパスを
通ること → IntentRepository/OrderManager/RiskEngine が実際に使われる。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from strata.api.models import OrderSide, PriceType, SendOrderRequest
from strata.backtest.broker import FillPolicy, PaperBroker
from strata.backtest.metrics import max_drawdown
from strata.backtest.runner import BacktestRunner, BacktestSettings
from strata.config import RiskSettings, WatchlistEntry
from strata.data.base import DailyBar
from strata.strategy.base import Signal, SignalAction, Strategy
from strata.strategy.moving_average import MovingAverageCross


def series(symbol: str, closes: list[float], start: date = date(2026, 1, 1)) -> list[DailyBar]:
    return [
        DailyBar(
            symbol=symbol,
            date=(start + timedelta(days=i)).isoformat(),
            open=c,
            high=c + 1,
            low=c - 1,
            close=c,
            volume=1000,
            source="manual",
        )
        for i, c in enumerate(closes)
    ]


class SingleDayBuyStrategy(Strategy):
    """初日に BUY 1 回だけ出す決定論的戦略（Runner 経路の検証用）."""

    name = "test_single_buy"

    def __init__(self) -> None:
        self._fired = False

    def generate_signals(self, bars) -> list[Signal]:  # type: ignore[no-untyped-def]
        if self._fired or not bars:
            return []
        self._fired = True
        symbol = next(iter(bars))
        return [Signal(symbol=symbol, action=SignalAction.BUY, reason="test buy")]


class AlwaysSellStrategy(Strategy):
    """常に SELL を出す（持っていない時の売却拒否経路の検証用）."""

    name = "test_always_sell"

    def generate_signals(self, bars) -> list[Signal]:  # type: ignore[no-untyped-def]
        return [Signal(symbol="8306", action=SignalAction.SELL, reason="test sell")]


class TestPaperBroker:
    def test_NEXT_OPENは翌営業日約定(self) -> None:
        broker = PaperBroker(initial_cash=1_000_000, fill=FillPolicy.NEXT_OPEN)
        broker.set_market_day("2026-01-05", {"8306": 100.0})
        req = SendOrderRequest(
            symbol="8306", side=OrderSide.BUY, qty=100, price_type=PriceType.MARKET
        )
        result = broker.sendorder(req)
        assert result.data.Result == 0
        # 当日は未約定
        assert broker.held_qty("8306") == 0
        # 翌営業日
        broker.set_market_day("2026-01-06", {"8306": 102.0})
        assert broker.held_qty("8306") == 100
        assert broker.cash == pytest.approx(1_000_000 - 102.0 * 100)

    def test_CLOSEは当日約定(self) -> None:
        broker = PaperBroker(initial_cash=1_000_000, fill=FillPolicy.CLOSE)
        broker.set_market_day("2026-01-05", {"8306": 100.0})
        req = SendOrderRequest(
            symbol="8306", side=OrderSide.BUY, qty=100, price_type=PriceType.MARKET
        )
        broker.sendorder(req)
        assert broker.held_qty("8306") == 100

    def test_買付可能額不足はResult21(self) -> None:
        broker = PaperBroker(initial_cash=1000, fill=FillPolicy.CLOSE)
        broker.set_market_day("2026-01-05", {"8306": 100.0})
        req = SendOrderRequest(
            symbol="8306", side=OrderSide.BUY, qty=100, price_type=PriceType.MARKET
        )
        result = broker.sendorder(req)
        assert not hasattr(result, "data") or result.data is None
        assert result.code == 21  # type: ignore[attr-defined]

    def test_現物なしの売りはResult21(self) -> None:
        broker = PaperBroker(initial_cash=1_000_000, fill=FillPolicy.CLOSE)
        broker.set_market_day("2026-01-05", {"8306": 100.0})
        req = SendOrderRequest(
            symbol="8306", side=OrderSide.SELL, qty=100, price_type=PriceType.MARKET
        )
        result = broker.sendorder(req)
        assert result.code == 21  # type: ignore[attr-defined]

    def test_売りで実現損益とwin_rate(self) -> None:
        broker = PaperBroker(initial_cash=1_000_000, fill=FillPolicy.CLOSE)
        broker.set_market_day("2026-01-05", {"8306": 100.0})
        broker.sendorder(
            SendOrderRequest(
                symbol="8306", side=OrderSide.BUY, qty=100, price_type=PriceType.MARKET
            )
        )
        broker.set_market_day("2026-01-06", {"8306": 110.0})
        broker.sendorder(
            SendOrderRequest(
                symbol="8306", side=OrderSide.SELL, qty=100, price_type=PriceType.MARKET
            )
        )
        assert broker.round_trips == 1
        assert broker.winning_exits == 1
        assert broker.realized_pnl == pytest.approx(1000.0)
        assert broker.held_qty("8306") == 0


class TestMetrics:
    def test_最大DD(self) -> None:
        assert max_drawdown([100, 80, 90, 70]) == pytest.approx(0.3)
        assert max_drawdown([100, 110, 120]) == 0.0
        assert max_drawdown([]) == 0.0


class TestBacktestRunner:
    def test_ダッシュボード基本的な健全性(self) -> None:
        bars = {"8306": series("8306", [100.0] * 20)}
        runner = BacktestRunner(
            strategy=SingleDayBuyStrategy(),
            universe=[WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)],
            bars=bars,
            risk_settings=RiskSettings(
                max_qty_per_order=1000,
                max_notional_per_order=1_000_000,
                max_open_positions=5,
                max_total_risk=100_000,
            ),
            settings=BacktestSettings(initial_cash=1_000_000, fill=FillPolicy.CLOSE),
        )
        result = runner.run()
        assert len(result.equity_curve) == 20
        assert result.metrics["executions"] >= 1
        # 1回だけBUY → 以降シグナルなし
        assert len(result.signals) == 1

    def test_実戦略MAクロスが動く(self) -> None:
        closes = [100.0, 95.0, 90.0, 85.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0]
        bars = {"8306": series("8306", closes)}
        runner = BacktestRunner(
            strategy=MovingAverageCross(fast=2, slow=4),
            universe=[WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)],
            bars=bars,
            settings=BacktestSettings(initial_cash=1_000_000, fill=FillPolicy.CLOSE),
        )
        result = runner.run()
        # いずれかの日で BUY シグナルが出て約定している
        assert any(s.action is SignalAction.BUY for s in result.signals)
        assert result.metrics["executions"] >= 1

    def test_保有なしの売りは発行されず異常なし(self) -> None:
        bars = {"8306": series("8306", [100.0] * 10)}
        runner = BacktestRunner(
            strategy=AlwaysSellStrategy(),
            universe=[WatchlistEntry(symbol="8306", trading_unit=100)],
            bars=bars,
            settings=BacktestSettings(initial_cash=1_000_000, fill=FillPolicy.CLOSE),
        )
        result = runner.run()
        # 売却は全て見送り（保有0）→約定0
        assert result.metrics["executions"] == 0
        assert result.rejected_signals >= 1

    def test_NEXT_OPENで日跨ぎ約定(self) -> None:
        bars = {"8306": series("8306", [100.0] * 10)}
        runner = BacktestRunner(
            strategy=SingleDayBuyStrategy(),
            universe=[WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)],
            bars=bars,
            settings=BacktestSettings(initial_cash=1_000_000, fill=FillPolicy.NEXT_OPEN),
        )
        result = runner.run()
        # 最終日に約定が反映されている
        assert result.metrics["executions"] >= 1
