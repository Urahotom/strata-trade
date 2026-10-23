"""リスクオフ・オーバーレイ（1306.T等の保有に重ねる「撤退判断」のpure関数群）.

F-004 の判定（常時ロングのシグナル戦略に優位性なし、あるとすれば DD と
回復期間を買うオーバーレイ）を受けた検証基盤。方針は古典的パラメータを
**一切いじらず**（学習期間での最適化を行わない）ことで、「パラメータを
直せば勝てる」の罠を最初から遮断する。

- 未来参照ゼロ: 各重みは**その日までの終値だけ**で決まる（当日終値時点で
  判定し、翌日のリターンに効かせる = 実行は翌日）。
- 重み w[i] は「バー i の終値で設定し、i→i+1 のリターンにかかる目標比率」。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import stdev

from strata.backtest.metrics import EquityPoint


def ma_filter_weights(closes: Sequence[float], *, window: int) -> list[float]:
    """終値が単純移動平均の上なら 1、下なら 0（classic 200日フィルタ）.

    本数が足りない期間は 0（現金）。境界（終値=平均）は「下」とみなす。
    """
    weights: list[float] = []
    total = 0.0
    for i, price in enumerate(closes):
        total += price
        if i >= window:
            total -= closes[i - window]
        if i < window - 1:
            weights.append(0.0)
            continue
        sma = total / window
        weights.append(1.0 if price > sma else 0.0)
    return weights


def ma_cross_weights(closes: Sequence[float], *, fast: int, slow: int) -> list[float]:
    """ファスト移動平均がスローの上なら 1、下なら 0（50/200 古典）."""
    if fast >= slow:
        raise ValueError("fast < slow が必須です")
    weights: list[float] = []
    fast_sum = 0.0
    slow_sum = 0.0
    for i, price in enumerate(closes):
        fast_sum += price
        slow_sum += price
        if i >= fast:
            fast_sum -= closes[i - fast]
        if i >= slow:
            slow_sum -= closes[i - slow]
        if i < slow - 1:
            weights.append(0.0)
            continue
        weights.append(1.0 if fast_sum / fast > slow_sum / slow else 0.0)
    return weights


def vol_target_weights(
    closes: Sequence[float], *, window: int, target_annual: float, trading_days: int = 244
) -> list[float]:
    """目標ボラに対する比率 w = min(1, target/vol)（volゼロは 1、未満走りは 0）.

    リターン標準偏差の単純年率換算。暴落時の自動減倉（vol targeting）。
    """
    weights: list[float] = []
    rets: list[float] = []
    for i, price in enumerate(closes):
        if i > 0 and closes[i - 1] > 0:
            rets.append(price / closes[i - 1] - 1.0)
        if len(rets) < window:
            weights.append(0.0)
            continue
        vol = stdev(rets[-window:]) * math.sqrt(trading_days)
        if vol <= 0:
            weights.append(1.0)
        else:
            weights.append(min(1.0, target_annual / vol))
    return weights


def simulate_weights(
    dates: Sequence[str],
    closes: Sequence[float],
    weights: Sequence[float],
    *,
    initial_cash: float,
    cost_rate: float = 0.0,
) -> list[EquityPoint]:
    """重み系列を日次リバランス（当日終値執行）でシミュレーションする.

    cost_rate は买卖それぞれにかかるコスト率（スリッページ等）。リバランス
    量 |Δw| × cost_rate をその日の資産から差し引く。w[i] は当日終値リターンに
    かからない（前日設定分が当日に効く）ため先読みは発生しない。
    """
    if not (len(dates) == len(closes) == len(weights)):
        raise ValueError("dates/closes/weights の長さが揃っていません")
    curve: list[EquityPoint] = []
    equity = initial_cash
    prev_w = 0.0
    for i, (day, close) in enumerate(zip(dates, closes, strict=True)):
        if i > 0 and closes[i - 1] > 0:
            equity *= 1.0 + prev_w * (close / closes[i - 1] - 1.0)
        turnover = abs(weights[i] - prev_w)
        if turnover > 0:
            equity -= turnover * cost_rate * equity
        market_value = weights[i] * equity
        curve.append(
            EquityPoint(
                date=day,
                cash=equity - market_value,
                market_value=market_value,
                equity=equity,
            )
        )
        prev_w = weights[i]
    return curve
