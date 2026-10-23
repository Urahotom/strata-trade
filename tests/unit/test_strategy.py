"""MAクロス戦略とシグナル基盤のテスト."""

from __future__ import annotations

import math

import pytest

from strata.data.base import DailyBar
from strata.strategy.base import Signal, SignalAction
from strata.strategy.moving_average import MovingAverageCross, sma


def closes_bars(closes: list[float], symbol: str = "8306") -> list[DailyBar]:
    return [
        DailyBar(
            symbol=symbol,
            date=f"2026-01-{i + 1:02d}",
            open=c,
            high=c,
            low=c,
            close=c,
            source="manual",
        )
        for i, c in enumerate(closes)
    ]


class TestSma:
    def test_直近n本の平均(self) -> None:
        assert sma([1, 2, 3, 4], 2) == 3.5

    def test_本数不足はValueError(self) -> None:
        with pytest.raises(ValueError):
            sma([1, 2], 3)

    def test_期間0はValueError(self) -> None:
        with pytest.raises(ValueError):
            sma([1], 0)


class TestMovingAverageCross:
    def test_パラメータ検証(self) -> None:
        with pytest.raises(ValueError):
            MovingAverageCross(fast=0)
        with pytest.raises(ValueError):
            MovingAverageCross(fast=25, slow=25)

    def test_本数不足ならシグナルなし(self) -> None:
        strategy = MovingAverageCross(fast=2, slow=5)
        bars = {"8306": closes_bars([100.0] * 5)}
        assert strategy.generate_signals(bars) == []

    def test_ゴールデンクロスでBUY(self) -> None:
        strategy = MovingAverageCross(fast=2, slow=4)
        # 横ばい→最終日で急騰し短期MAが長期MAを上抜ける
        closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 130.0]
        signals = strategy.generate_signals({"8306": closes_bars(closes)})
        assert len(signals) == 1
        assert signals[0].action is SignalAction.BUY
        assert signals[0].symbol == "8306"
        assert "ゴールデンクロス" in signals[0].reason

    def test_デッドクロスでSELL(self) -> None:
        strategy = MovingAverageCross(fast=2, slow=4)
        closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 70.0]
        signals = strategy.generate_signals({"8306": closes_bars(closes)})
        assert len(signals) == 1
        assert signals[0].action is SignalAction.SELL

    def test_横ばいはシグナルなし(self) -> None:
        strategy = MovingAverageCross(fast=2, slow=4)
        closes = [100.0] * 10
        assert strategy.generate_signals({"8306": closes_bars(closes)}) == []

    def test_複数銘柄を独立判定(self) -> None:
        strategy = MovingAverageCross(fast=2, slow=4)
        up = closes_bars([100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 130.0], "8306")
        flat = closes_bars([100.0] * 10, "9433")
        signals = strategy.generate_signals({"8306": up, "9433": flat})
        assert [s.symbol for s in signals] == ["8306"]

    def test_戦略は発注手段を持たない_DESIGN原則(self) -> None:
        # Strategy の公開メソッドは generate_signals のみ（クライアントを持たない）
        strategy = MovingAverageCross()
        public = {m for m in dir(strategy) if not m.startswith("_")}
        assert "sendorder" not in public
        assert "submit" not in public
        assert "generate_signals" in public
        assert not math.isnan(0.0)


class TestSignalModel:
    def test_既定値(self) -> None:
        s = Signal(symbol="8306", action=SignalAction.BUY)
        assert s.strength == 1.0
        assert s.reason == ""
