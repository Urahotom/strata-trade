"""意図台帳リポジトリのテスト."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from strata.api.models import OrderSide, PriceType
from strata.order.repository import Intent, IntentRepository
from strata.order.states import InvalidTransitionError, OrderState
from strata.store import init_db


@pytest.fixture()
def repo(tmp_path: Path) -> IntentRepository:
    conn = init_db(tmp_path / "repo.db")
    yield IntentRepository(conn)
    conn.close()


def make_intent(symbol: str = "8306", side: OrderSide = OrderSide.BUY) -> Intent:
    return Intent.new(
        trade_date="2026-10-23",
        symbol=symbol,
        market=9,
        side=side,
        qty=100,
        price_type=PriceType.MARKET,
    )


class TestCreate:
    def test_作成はPLANNEDとINTENT_CREATEDを残す(self, repo: IntentRepository) -> None:
        intent = make_intent()
        repo.create(intent)
        loaded = repo.get(intent.intent_id)
        assert loaded is not None
        assert loaded.state is OrderState.PLANNED
        assert loaded.side is OrderSide.BUY
        events = repo.events_for(intent.intent_id)
        assert [e["event_type"] for e in events] == ["INTENT_CREATED"]

    def test_同一intent_idの二重作成は制約エラー(self, repo: IntentRepository) -> None:
        intent = make_intent()
        repo.create(intent)
        with pytest.raises(sqlite3.IntegrityError):
            repo.create(intent)


class TestTransition:
    def test_正常遷移とorder_idの反映(self, repo: IntentRepository) -> None:
        intent = make_intent()
        repo.create(intent)
        updated = repo.transition(intent.intent_id, OrderState.SENT, event_type="SENT")
        assert updated.state is OrderState.SENT
        updated = repo.transition(
            intent.intent_id, OrderState.ACK, event_type="ACCEPTED", order_id="OID-1"
        )
        assert updated.order_id == "OID-1"
        events = [e["event_type"] for e in repo.events_for(intent.intent_id)]
        assert events == ["INTENT_CREATED", "SENT", "ACCEPTED"]

    def test_不正遷移は例外で台帳は不変(self, repo: IntentRepository) -> None:
        intent = make_intent()
        repo.create(intent)
        with pytest.raises(InvalidTransitionError):
            repo.transition(intent.intent_id, OrderState.FILLED, event_type="HACK")
        assert repo.get(intent.intent_id) is not None
        assert repo.get(intent.intent_id).state is OrderState.PLANNED  # type: ignore[union-attr]
        events = [e["event_type"] for e in repo.events_for(intent.intent_id)]
        assert "HACK" not in events  # ロールバックされている

    def test_reject情報を記録できる(self, repo: IntentRepository) -> None:
        intent = make_intent()
        repo.create(intent)
        repo.transition(intent.intent_id, OrderState.SENT, event_type="SENT")
        updated = repo.transition(
            intent.intent_id,
            OrderState.REJECTED,
            event_type="ORDER_REJECTED",
            reject_code=21,
            reject_message="買付可能額不足",
        )
        assert updated.reject_code == 21
        assert updated.reject_message == "買付可能額不足"

    def test_SENTからPLANNEDへの巻き戻しができる(self, repo: IntentRepository) -> None:
        """突合で未到着と確認したケース（二重発注防止の中核遷移）."""
        intent = make_intent()
        repo.create(intent)
        repo.transition(intent.intent_id, OrderState.SENT, event_type="SENT")
        updated = repo.transition(
            intent.intent_id, OrderState.PLANNED, event_type="RECONCILE_NOT_FOUND"
        )
        assert updated.state is OrderState.PLANNED


class TestQueries:
    def test_find_activeは終端以外を返す(self, repo: IntentRepository) -> None:
        a = make_intent("8306")
        b = make_intent("9433")
        repo.create(a)
        repo.create(b)
        repo.transition(a.intent_id, OrderState.SENT, event_type="SENT")
        repo.transition(a.intent_id, OrderState.ACK, event_type="ACK")
        active = repo.find_active(trade_date="2026-10-23", symbol="8306")
        assert [i.intent_id for i in active] == [a.intent_id]

        repo.transition(a.intent_id, OrderState.WORKING, event_type="W")
        repo.transition(a.intent_id, OrderState.FILLED, event_type="F")
        assert repo.find_active(trade_date="2026-10-23", symbol="8306") == []

    def test_find_activeは売買方向で絞り込める(self, repo: IntentRepository) -> None:
        buy = make_intent("8306", OrderSide.BUY)
        sell = make_intent("8306", OrderSide.SELL)
        repo.create(buy)
        repo.create(sell)
        assert (
            repo.find_active(trade_date="2026-10-23", symbol="8306", side=OrderSide.BUY)[
                0
            ].intent_id
            == buy.intent_id
        )
        assert (
            repo.find_active(trade_date="2026-10-23", symbol="8306", side=OrderSide.SELL)[
                0
            ].intent_id
            == sell.intent_id
        )

    def test_claimed_order_ids(self, repo: IntentRepository) -> None:
        intent = make_intent()
        repo.create(intent)
        repo.transition(intent.intent_id, OrderState.SENT, event_type="S")
        repo.transition(intent.intent_id, OrderState.ACK, event_type="A", order_id="OID-7")
        assert repo.claimed_order_ids() == {"OID-7"}

    def test_count_open_symbolsとsum_committed_risk(self, repo: IntentRepository) -> None:
        a = make_intent("8306")
        b = make_intent("9433")
        repo.create(a)
        repo.create(b)
        repo.append_event(a.intent_id, "RISK_APPROVED", {"est_risk": 10000.0})
        repo.append_event(b.intent_id, "RISK_APPROVED", {"est_risk": 5000.0})
        assert repo.count_open_symbols("2026-10-23") == 2
        assert repo.sum_committed_risk("2026-10-23") == 15000.0


class TestExecutions:
    def test_約定はexecution_idで冪等(self, repo: IntentRepository) -> None:
        intent = make_intent()
        repo.create(intent)
        kwargs = dict(
            intent_id=intent.intent_id,
            order_id="OID-1",
            trade_date="2026-10-23",
            traded_at="20261023090000",
            symbol="8306",
            market=9,
            side="BUY",
            price=1000.0,
            qty=100,
        )
        assert repo.record_execution(execution_id="X-1", **kwargs) is True  # type: ignore[arg-type]
        assert repo.record_execution(execution_id="X-1", **kwargs) is False  # type: ignore[arg-type]
        assert len(repo.executions_for(intent.intent_id)) == 1

    def test_建玉スナップショットの保存(self, repo: IntentRepository) -> None:
        captured = "2026-10-23T09:00:00+09:00"
        n = repo.add_positions_snapshot(
            captured,
            [{"Symbol": "8306", "Exchange": 1, "Side": "2", "HoldQty": 100, "Price": 1000}],
        )
        assert n == 1
        rows = repo.list_positions_snapshot(captured)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "8306"
        assert rows[0]["side"] == "BUY"  # 公式 "2"=買 を正規化
