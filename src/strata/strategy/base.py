"""③ 戦略プラグイン基盤: シグナル出力のみ。発注・リスクを呼べない."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from strata.data.base import DailyBar


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Signal:
    """戦略が出す「発注のお願い」。執行可否はリスク・注文層が判断する."""

    symbol: str
    action: SignalAction
    #: 根拠（監査ログ・メール通知に載せる説明文字列）
    reason: str = ""
    #: シグナル強度 0.0〜1.0（既定1.0。サイジングの係数には現状使わない）
    strength: float = 1.0


class Strategy(ABC):
    """戦略基底クラス。

    引数にブローカークライアントを取れない設計により、戦略が直接
    発注できないことをシグネチャレベルで担保する（DESIGN §1.3）。
    """

    #: 台帳・ログにのる識別子
    name: ClassVar[str] = "strategy"

    @abstractmethod
    def generate_signals(self, bars: Mapping[str, Sequence[DailyBar]]) -> list[Signal]:
        """銘柄ごとの（日付昇順・検証済み）日足からシグナルを生成する."""
