"""テクニカル指標のpure関数集（標準ライブラリのみ・未来参照なし）.

すべての関数は「その日までの本数」だけで計算する（バイアス防止）。
計算不足の本数で呼ばれた場合は例外（ValueError）。戦略側は長さを見て呼ぶ。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def sma(values: Sequence[float], n: int) -> float:
    """直近n本の単純移動平均。n本満たなければValueError."""
    if n <= 0:
        raise ValueError("期間nは1以上です")
    if len(values) < n:
        raise ValueError(f"移動平均に不足する本数です: {len(values)} < {n}")
    return sum(values[-n:]) / n


def rsi(closes: Sequence[float], period: int = 14) -> float:
    """Wilder方式のRSI（0〜100）。period+1本以上が必要."""
    if period < 1:
        raise ValueError("period は1以上です")
    if len(closes) < period + 1:
        raise ValueError(f"RSI に不足する本数です: {len(closes)} < {period + 1}")
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


@dataclass(frozen=True)
class BollingerBands:
    """ボリンジャーバンド（中心sma・母集団標準偏差）."""

    mid: float
    upper: float
    lower: float


def bollinger(closes: Sequence[float], period: int = 20, k: float = 2.0) -> BollingerBands:
    """直近period本のバンド。period本以上必要。"""
    if period < 2:
        raise ValueError("period は2以上です")
    if k <= 0:
        raise ValueError("k は正数です")
    if len(closes) < period:
        raise ValueError(f"バンドに不足する本数です: {len(closes)} < {period}")
    window = closes[-period:]
    mid = sum(window) / period
    sd = math.sqrt(sum((v - mid) ** 2 for v in window) / period)
    return BollingerBands(mid=mid, upper=mid + k * sd, lower=mid - k * sd)


@dataclass(frozen=True)
class DonchianChannel:
    """ドンチアンチャンネル（直近period本の最高値・最安値）。"""

    upper: float
    lower: float


def donchian(highs: Sequence[float], lows: Sequence[float], period: int = 20) -> DonchianChannel:
    """直近period本のレンジ。period本以上必要。"""
    if period < 1:
        raise ValueError("period は1以上です")
    if len(highs) != len(lows):
        raise ValueError("highs と lows は同長です")
    if len(highs) < period:
        raise ValueError(f"チャンネルに不足する本数です: {len(highs)} < {period}")
    return DonchianChannel(upper=max(highs[-period:]), lower=min(lows[-period:]))


def previous_channel_excluding_today(
    highs: Sequence[float], lows: Sequence[float], period: int
) -> DonchianChannel:
    """「本日を含まない」直近period本のレンジ（ブレイク判定の基準）.

    本日の高値・安値が基準に含まれると「本日の最高値 ≤ 本日の最高値」で
    ブレイクが永遠に成立しないため、必ず1本ずらして使う。
    """
    return donchian(highs[:-1], lows[:-1], period)
