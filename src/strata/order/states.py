"""注文ライフサイクルの状態定義.

状態機械:
    PLANNED -> SENT -> ACK -> WORKING -> PART_FILLED -> FILLED
                                              (-> CANCELED | REJECTED | EXPIRED)

- PLANNED: 意図のみDBに存在し、まだ送信していない
- SENT:    送信したが応答未確認（タイムアウト等では SENT のまま突合待ち）
- ACK:     発注受理（OrderId 取得済み）
- WORKING: 取引所受付済み・有効期間内
- PART_FILLED: 一部約定（CumQty < OrderQty）
- FILLED / CANCELED / REJECTED / EXPIRED: 終端状態
"""

from __future__ import annotations

from enum import StrEnum


class OrderState(StrEnum):
    PLANNED = "PLANNED"
    SENT = "SENT"
    ACK = "ACK"
    WORKING = "WORKING"
    PART_FILLED = "PART_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)


ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PLANNED: frozenset({OrderState.SENT, OrderState.CANCELED}),
    # SENT: 応答喪失時は SENT のまま残り、突合で ACK / PLANNED(未到着確定) / REJECTED へ
    OrderState.SENT: frozenset(
        {
            OrderState.ACK,
            OrderState.REJECTED,
            OrderState.CANCELED,
            OrderState.PLANNED,
        }
    ),
    OrderState.ACK: frozenset(
        {
            OrderState.WORKING,
            OrderState.REJECTED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.WORKING: frozenset(
        {
            OrderState.PART_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PART_FILLED: frozenset(
        {
            OrderState.PART_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
        }
    ),
}


class InvalidTransitionError(Exception):
    """許可されていない状態遷移."""

    def __init__(self, src: OrderState, dst: OrderState) -> None:
        super().__init__(f"不正な注文状態遷移: {src} -> {dst}")
        self.src = src
        self.dst = dst


def can_transition(src: OrderState, dst: OrderState) -> bool:
    """状態遷移が許可されているかを返す（終端状態からの遷移は常に False）."""
    if src in TERMINAL_STATES:
        return False
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: OrderState, dst: OrderState) -> None:
    """状態遷移が不正なら InvalidTransitionError を送出する."""
    if not can_transition(src, dst):
        raise InvalidTransitionError(src, dst)
