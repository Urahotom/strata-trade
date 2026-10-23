"""テクニカル戦略3種（technical.py）の単体テスト.

TESTING §3.7: いずれも「状態遷移の当日だけ」シグナルを出すこと、
本数不足では何も出さないことを確認する。
"""

from __future__ import annotations

from datetime import date, timedelta

from strata.data.base import DailyBar
from strata.strategy.base import SignalAction
from strata.strategy.technical import (
    BollingerMeanReversion,
    DonchianBreakout,
    RSIMeanReversion,
)

START = date(2024, 1, 1)


def bars_of(symbol: str, closes: list[float]) -> list[DailyBar]:
    out: list[DailyBar] = []
    d = START
    for i, c in enumerate(closes):
        out.append(
            DailyBar(
                symbol=symbol,
                date=(d + timedelta(days=i)).isoformat(),
                open=c,
                high=c * 1.005,
                low=c * 0.995,
                close=c,
                volume=1000,
                source="manual",
            )
        )
    return out


def daily(closes: list[float]) -> dict[str, list[DailyBar]]:
    return {"8306": bars_of("8306", closes)}


class TestRSIMeanReversion:
    def test_売られすぎ復帰でBUY(self) -> None:
        strategy = RSIMeanReversion(period=3, lower=30.0, upper=70.0)
        closes = [100.0 - i for i in range(15)]  # 一本調子の下落 → RSI 0
        for series_len in range(6, len(closes) + 1):
            assert strategy.generate_signals(daily(closes[:series_len])) == []
        rebound = [*closes, closes[-1] + 40.0]
        signals = strategy.generate_signals(daily(rebound))
        assert [s.action for s in signals] == [SignalAction.BUY]
        assert "RSI" in signals[0].reason

    def test_買われすぎ復帰でSELL(self) -> None:
        strategy = RSIMeanReversion(period=3, lower=30.0, upper=70.0)
        closes = [100.0 + i for i in range(15)]
        pull = [*closes, closes[-1] - 40.0]
        signals = strategy.generate_signals(daily(pull))
        assert [s.action for s in signals] == [SignalAction.SELL]

    def test_本数不足は何も出さない(self) -> None:
        strategy = RSIMeanReversion(period=14)
        assert strategy.generate_signals(daily([100.0] * 10)) == []

    def test_閾値内継続では連発しない(self) -> None:
        strategy = RSIMeanReversion(period=3, lower=30.0, upper=70.0)
        closes = [100.0 - i for i in range(15)] + [10.0, 12.0, 14.0, 16.0]
        # 復帰日は1回だけ。翌日以降は BUY が出ないことを確認
        day_signals = [
            strategy.generate_signals(daily(closes[:n])) for n in range(17, len(closes) + 1)
        ]
        buys = [s for sigs in day_signals for s in sigs if s.action is SignalAction.BUY]
        assert len(buys) <= 1


class TestBollingerMeanReversion:
    def test_下限割れ復帰でBUY(self) -> None:
        strategy = BollingerMeanReversion(period=20, k=2.0)
        closes = [5000.0] * 30 + [4800.0]
        # 割れ当日はまだ BUY ではない（前日割れが条件）
        assert strategy.generate_signals(daily(closes)) == []
        recovery = [*closes, 5000.0]
        signals = strategy.generate_signals(daily(recovery))
        assert [s.action for s in signals] == [SignalAction.BUY]
        assert "ボリンジャー" in signals[0].reason

    def test_上限超え復帰でSELL(self) -> None:
        strategy = BollingerMeanReversion(period=20, k=2.0)
        closes = [5000.0] * 30 + [5200.0, 5000.0]
        signals = strategy.generate_signals(daily(closes))
        assert [s.action for s in signals] == [SignalAction.SELL]

    def test_平値では何もしない(self) -> None:
        strategy = BollingerMeanReversion(period=20)
        assert strategy.generate_signals(daily([5000.0] * 60)) == []


class TestDonchianBreakout:
    def test_N本高値超えでBUY(self) -> None:
        strategy = DonchianBreakout(entry=20, exit=10)
        closes = [5000.0] * 30
        assert strategy.generate_signals(daily(closes)) == []
        breakout = [*closes, 5100.0]
        signals = strategy.generate_signals(daily(breakout))
        assert [s.action for s in signals] == [SignalAction.BUY]
        assert "ドンチアン" in signals[0].reason

    def test_決済は短期N本安値割れでSELL(self) -> None:
        strategy = DonchianBreakout(entry=20, exit=10)
        closes = [5000.0] * 30 + [5100.0] + [5050.0] * 12
        crash = [*closes, 4900.0]
        signals = strategy.generate_signals(daily(crash))
        assert [s.action for s in signals] == [SignalAction.SELL]

    def test_当日高値は基準に含まれない(self) -> None:
        # 本日の high を基準に含めると永遠に成立しないはずの条件
        strategy = DonchianBreakout(entry=20, exit=10)
        closes = [5000.0] * 25
        signals = strategy.generate_signals(daily([*closes, 5000.0]))
        assert signals == []
