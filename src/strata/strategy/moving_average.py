"""移動平均線クロス戦略（初期戦略・DESIGN §1 既定）."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from strata.data.base import DailyBar
from strata.strategy.base import Signal, SignalAction, Strategy
from strata.strategy.indicators import sma


class MovingAverageCross(Strategy):
    """終値の短期/長期移動平均がクロスした日にシグナルを出す.

    - ゴールデンクロス（短期が長期を上抜け）→ BUY
    - デッドクロス（短期が長期を下抜け）→ SELL
    クロス判定には長期本数+1本以上の終値が必要。不足銘柄は何も出さない。
    """

    name = "ma_cross"

    def __init__(self, fast: int = 5, slow: int = 25) -> None:
        if fast < 1:
            raise ValueError("短期期間は1以上です")
        if slow <= fast:
            raise ValueError("長期期間は短期期間より大きくしてください")
        self._fast = fast
        self._slow = slow

    def generate_signals(self, bars: Mapping[str, Sequence[DailyBar]]) -> list[Signal]:
        signals: list[Signal] = []
        for symbol, series in bars.items():
            closes = [b.close for b in series]
            if len(closes) < self._slow + 1:
                continue
            prev_fast = sma(closes[:-1], self._fast)
            prev_slow = sma(closes[:-1], self._slow)
            cur_fast = sma(closes, self._fast)
            cur_slow = sma(closes, self._slow)
            desc = f"MA{self._fast}/{self._slow}: {prev_fast:.2f}->{cur_fast:.2f} vs {cur_slow:.2f}"
            if prev_fast <= prev_slow and cur_fast > cur_slow:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.BUY,
                        reason=f"ゴールデンクロス {desc}",
                    )
                )
            elif prev_fast >= prev_slow and cur_fast < cur_slow:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.SELL,
                        reason=f"デッドクロス {desc}",
                    )
                )
        return signals
