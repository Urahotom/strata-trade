"""③ 戦略プラグイン（シグナル出力のみ・発注権限なし）."""

from strata.strategy.base import Signal, SignalAction, Strategy
from strata.strategy.moving_average import MovingAverageCross, sma

__all__ = ["MovingAverageCross", "Signal", "SignalAction", "Strategy", "sma"]
