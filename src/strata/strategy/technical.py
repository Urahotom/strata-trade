"""テクニカル指標ベースの戦略群（RSI・ボリンジャー・ドンチアン）.

いずれも「その日までの日足だけ」で判定し、**状態遷移の当日**だけシグナルを
出す（閾値内である間、毎日出し続けない）。発注権限は持たない（Strategy基底）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from strata.data.base import DailyBar
from strata.strategy.base import Signal, SignalAction, Strategy
from strata.strategy.indicators import (
    bollinger,
    previous_channel_excluding_today,
    rsi,
)


class RSIMeanReversion(Strategy):
    """RSI売られすぎ→買われ戻し戦略.

    - 前日が下限（既定30）未満 → 当日が下限以上へ復帰した日に BUY
    - 前日が上限（既定70）超え → 当日が上限以下へ復帰した日に SELL
    （閾値「内」ではなく「復帰.cross」で出すため、押し目での連続発注を避ける）
    """

    name = "rsi_reversion"

    def __init__(self, period: int = 14, lower: float = 30.0, upper: float = 70.0) -> None:
        if period < 2:
            raise ValueError("period は2以上です")
        if not 0 < lower < upper < 100:
            raise ValueError("0 < lower < upper < 100 を満たす閾値が必要です")
        self._period = period
        self._lower = lower
        self._upper = upper

    def generate_signals(self, bars: Mapping[str, Sequence[DailyBar]]) -> list[Signal]:
        signals: list[Signal] = []
        for symbol, series in bars.items():
            closes = [b.close for b in series]
            if len(closes) < self._period + 2:
                continue
            prev = rsi(closes[:-1], self._period)
            cur = rsi(closes, self._period)
            desc = f"RSI{self._period}: {prev:.1f}->{cur:.1f}"
            if prev < self._lower <= cur:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.BUY,
                        reason=f"RSI Oversold recovery {desc} (>= {self._lower:.0f})",
                    )
                )
            elif prev > self._upper >= cur:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.SELL,
                        reason=f"RSI Overbought recovery {desc} (<= {self._upper:.0f})",
                    )
                )
        return signals


class BollingerMeanReversion(Strategy):
    """ボリンジャーバンド・バンドタッチ復帰戦略.

    - 前日終値が下限バンド割れ → 当日が復帰した日に BUY
    - 前日終値が上限バンド超え → 当日が復帰した日に SELL
    """

    name = "bollinger_reversion"

    def __init__(self, period: int = 20, k: float = 2.0) -> None:
        if period < 2:
            raise ValueError("period は2以上です")
        if k <= 0:
            raise ValueError("k は正数です")
        self._period = period
        self._k = k

    def generate_signals(self, bars: Mapping[str, Sequence[DailyBar]]) -> list[Signal]:
        signals: list[Signal] = []
        for symbol, series in bars.items():
            closes = [b.close for b in series]
            if len(closes) < self._period + 1:
                continue
            prev_close = closes[-2]
            prev_band = bollinger(closes[:-1], self._period, self._k)
            cur_band = bollinger(closes, self._period, self._k)
            cur_close = closes[-1]
            desc = (
                f"BB{self._period}/{self._k:g}: "
                f"close {prev_close:.1f}->{cur_close:.1f} "
                f"band[{cur_band.lower:.1f},{cur_band.upper:.1f}]"
            )
            if prev_close < prev_band.lower and cur_close >= cur_band.lower:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.BUY,
                        reason=f"ボリンジャー下限復帰 {desc}",
                    )
                )
            elif prev_close > prev_band.upper and cur_close <= cur_band.upper:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.SELL,
                        reason=f"ボリンジャー上限復帰 {desc}",
                    )
                )
        return signals


class DonchianBreakout(Strategy):
    """ドンチアン・ブレイクアウト戦略.

    - 終値が「本日を除く直近entry本」の最高値超え → BUY（順張りブレイク）
    - 終値が「本日を除く直近exit本」の最安値割れ → SELL（決済は買いより短め）
    """

    name = "donchian_breakout"

    def __init__(self, entry: int = 20, exit: int = 10) -> None:
        if entry < 2:
            raise ValueError("entry は2以上です")
        if not 1 <= exit <= entry:
            raise ValueError("exit は 1以上entry以下 です")
        self._entry = entry
        self._exit = exit

    def generate_signals(self, bars: Mapping[str, Sequence[DailyBar]]) -> list[Signal]:
        signals: list[Signal] = []
        for symbol, series in bars.items():
            highs = [b.high for b in series]
            lows = [b.low for b in series]
            closes = [b.close for b in series]
            if len(closes) < self._entry + 1:
                continue
            cur_close = closes[-1]
            buy_channel = previous_channel_excluding_today(highs, lows, self._entry)
            sell_channel = previous_channel_excluding_today(highs, lows, self._exit)
            if cur_close > buy_channel.upper:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.BUY,
                        reason=(
                            f"ドンチアン{self._entry}本ブレイク上 "
                            f"close {cur_close:.1f} > {buy_channel.upper:.1f}"
                        ),
                    )
                )
            elif cur_close < sell_channel.lower:
                signals.append(
                    Signal(
                        symbol=symbol,
                        action=SignalAction.SELL,
                        reason=(
                            f"ドンチアン{self._exit}本ブレイク下 "
                            f"close {cur_close:.1f} < {sell_channel.lower:.1f}"
                        ),
                    )
                )
        return signals
