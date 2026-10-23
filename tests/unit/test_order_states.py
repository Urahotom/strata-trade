"""注文状態機械（P0: 遷移テーブル）のユニットテスト."""

from __future__ import annotations

import itertools

import pytest

from strata.order.states import (
    TERMINAL_STATES,
    InvalidTransitionError,
    OrderState,
    assert_transition,
    can_transition,
)


def test_正常フローの遷移はすべて許可() -> None:
    flow = [
        OrderState.PLANNED,
        OrderState.SENT,
        OrderState.ACK,
        OrderState.WORKING,
        OrderState.PART_FILLED,
        OrderState.FILLED,
    ]
    for src, dst in itertools.pairwise(flow):
        assert can_transition(src, dst), f"{src} -> {dst} が許可されていない"
        assert_transition(src, dst)


def test_終端状態からの遷移はすべて不許可() -> None:
    for terminal in TERMINAL_STATES:
        for dst in OrderState:
            assert not can_transition(terminal, dst)


def test_送信喪失から意図への巻き戻しは許可_SENTからPLANNED() -> None:
    assert can_transition(OrderState.SENT, OrderState.PLANNED)


def test_不正遷移は例外() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_transition(OrderState.PLANNED, OrderState.FILLED)
    with pytest.raises(InvalidTransitionError):
        assert_transition(OrderState.FILLED, OrderState.WORKING)


def test_StrEnumはDBのチェック制約と文字列が一致() -> None:
    # migrations/0001_init.sql の CHECK (state IN (...)) と同一であること
    assert {s.value for s in OrderState} == {
        "PLANNED",
        "SENT",
        "ACK",
        "WORKING",
        "PART_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
    }
