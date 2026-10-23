"""OrderManager のユニットテスト（クライアントはフェイク、DBは一時実物）.

TESTING.md §3.2 のケースをカバーする:
応答喪失→突合、未到着→巻き戻し→再送、リジェクト不動作、
重複/クロス阻止、部分約定残余取消、再起動リコンサイル、オーファン停止。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.api.models import (
    OrderDetailItem,
    OrderItem,
    OrderResult,
    OrderSide,
    PositionItem,
    PositionsResponse,
    PriceType,
    SendOrderRequest,
)
from strata.api.results import (
    NETWORK_KIND_PROTOCOL,
    NETWORK_KIND_TIMEOUT,
    ApiFailure,
    ApiSuccess,
    HttpFailure,
    NetworkFailure,
)
from strata.config import RiskSettings
from strata.notify.base import InMemoryNotifier, Severity
from strata.order.manager import (
    CrossOrderError,
    DuplicateIntentError,
    KillSwitchActiveError,
    OrderManager,
    OrderManagerError,
    PartialFillPolicy,
    RiskBlockedError,
)
from strata.order.repository import IntentRepository
from strata.order.states import OrderState
from strata.risk.checks import RiskEngine
from strata.risk.killswitch import KillSwitch
from strata.store import init_db

DATE = "2026-10-23"


class FakeClient:
    """KabuClient ダミー（発注結果・照会結果をスクリプト）."""

    def __init__(self) -> None:
        self.send_queue: list[object] = []
        self.orders_by_id: dict[str, list[OrderItem]] = {}
        self.orders_by_symbol: dict[str, list[OrderItem]] = {}
        self.orders_result: object | None = None  # 設定時はこれを返す
        self.cancel_results: list[object] = []
        self.positions_result: object = ApiSuccess(PositionsResponse(Holdings=[]))
        self.sent: list[SendOrderRequest] = []
        self.cancelled: list[str] = []

    def sendorder(self, request: SendOrderRequest) -> object:
        self.sent.append(request)
        return self.send_queue.pop(0)

    def orders(self, **kw: object) -> object:
        if self.orders_result is not None:
            return self.orders_result
        order_id = kw.get("order_id")
        if order_id:
            return ApiSuccess(self.orders_by_id.get(str(order_id), []))
        symbol = kw.get("symbol")
        if symbol:
            return ApiSuccess(self.orders_by_symbol.get(str(symbol), []))
        return ApiSuccess([])

    def cancelorder(self, order_id: str) -> object:
        self.cancelled.append(order_id)
        if self.cancel_results:
            return self.cancel_results.pop(0)
        return ApiSuccess(OrderResult(Result=0, OrderId=order_id))

    def positions(self) -> object:
        return self.positions_result


def item(
    order_id: str = "OID-1",
    symbol: str = "8306",
    side: str = "2",
    qty: int = 100,
    cum: int = 0,
    state: int = 5,
    price: float = 0.0,
    details: list[dict] | None = None,
    recv: str = "20261023090000",
) -> OrderItem:
    return OrderItem(
        ID=order_id,
        State=state,
        Symbol=symbol,
        Side=side,
        OrderQty=qty,
        CumQty=cum,
        Price=price,
        RecvTime=recv,
        Details=[OrderDetailItem(**d) for d in (details or [])],
    )


def fill_detail(qty: int, price: float = 1000.0, exec_id: str = "X-1") -> dict:
    return {
        "SeqNum": 2,
        "RecType": 8,
        "Qty": qty,
        "Price": price,
        "ExecutionID": exec_id,
        "TransactTime": "20261023090100",
    }


def request(side: OrderSide = OrderSide.BUY, qty: int = 100) -> SendOrderRequest:
    return SendOrderRequest(symbol="8306", side=side, qty=qty, price_type=PriceType.MARKET)


def net_failure() -> NetworkFailure:
    return NetworkFailure(
        kind=NETWORK_KIND_TIMEOUT,
        message="timed out",
        attempts=1,
        send_may_have_reached=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> IntentRepository:
    conn = init_db(tmp_path / "manager.db")
    yield IntentRepository(conn)
    conn.close()


@pytest.fixture()
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture()
def notifier() -> InMemoryNotifier:
    return InMemoryNotifier()


@pytest.fixture()
def manager(repo: IntentRepository, client: FakeClient, notifier: InMemoryNotifier) -> OrderManager:
    return OrderManager(repo, client, notifier)  # type: ignore[arg-type]


class TestSubmit:
    def test_受付で意図がACKになりOrderIdを記録(self, manager, repo, client, notifier) -> None:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.ACK
        assert intent.order_id == "OID-1"
        events = [e["event_type"] for e in repo.events_for(intent.intent_id)]
        assert events == ["INTENT_CREATED", "SENT", "ORDER_ACCEPTED"]
        assert notifier.subjects(Severity.INFO)  # 受付通知

    def test_リスク拒否では意図すら作成されない(self, repo, client, notifier) -> None:
        engine = RiskEngine(RiskSettings())
        manager = OrderManager(repo, client, notifier, risk_engine=engine)  # type: ignore[arg-type]
        with pytest.raises(RiskBlockedError):
            manager.submit(request(), trade_date=DATE, ref_price=1000)  # stop_width無し
        assert client.sent == []
        assert repo.list_intents_for_date(DATE) == []

    def test_同一方向の重複発注を阻止(self, manager, client) -> None:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        manager.submit(request(), trade_date=DATE, ref_price=1000)
        with pytest.raises(DuplicateIntentError):
            manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert len(client.sent) == 1

    def test_反対方向のクロスを阻止(self, manager, client) -> None:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        manager.submit(request(), trade_date=DATE, ref_price=1000)
        with pytest.raises(CrossOrderError):
            manager.submit(request(side=OrderSide.SELL), trade_date=DATE, ref_price=1000)
        assert len(client.sent) == 1

    def test_キルスイッチ作動中は発注不可(self, repo, client, notifier) -> None:
        ks = KillSwitch(max_daily_loss=1)
        ks.activate("訓練")
        manager = OrderManager(repo, client, notifier, kill_switch=ks)  # type: ignore[arg-type]
        with pytest.raises(KillSwitchActiveError):
            manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert client.sent == []


class TestUnconfirmedSend:
    def test_応答喪失から突合で到達確認し自動再送しない(
        self, manager, repo, client, notifier
    ) -> None:
        client.send_queue.append(net_failure())
        broker = item(state=3)
        client.orders_by_symbol["8306"] = [broker]

        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.ACK
        assert intent.order_id == "OID-1"
        assert len(client.sent) == 1  # 絶対に1回しか送っていない
        assert notifier.subjects(Severity.ALERT)  # 復旧の報せ

    def test_突合で未到着ならPLANNEDへ巻き戻し再送可能(self, manager, repo, client) -> None:
        client.send_queue.append(net_failure())
        client.orders_by_symbol["8306"] = []  # 到達していない

        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.PLANNED
        assert len(client.sent) == 1

        # 未到着が確認できたので retry で再送してよい
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-2")))
        client.orders_by_id["OID-2"] = [item(order_id="OID-2", state=3)]
        retried = manager.retry(intent.intent_id, ref_price=1000)
        assert retried.state is OrderState.ACK
        assert len(client.sent) == 2

    def test_突合自体が失敗したらSENTのまま(self, manager, repo, client) -> None:
        client.send_queue.append(net_failure())
        client.orders_result = NetworkFailure(kind=NETWORK_KIND_PROTOCOL, message="down")
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.SENT
        events = [e["event_type"] for e in repo.events_for(intent.intent_id)]
        assert "RECONCILE_FAILED" in events
        assert len(client.sent) == 1

    def test_リジェクトは終端で自動再発注しない(self, manager, repo, client, notifier) -> None:
        client.send_queue.append(ApiFailure(code=21, message="買付可能額不足", kind="order"))
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.REJECTED
        assert intent.reject_code == 21
        assert notifier.subjects(Severity.ALERT)
        assert len(client.sent) == 1
        with pytest.raises(OrderManagerError):
            manager.retry(intent.intent_id, ref_price=1000)
        # 別の新しい意図としての再挑戦は可能（終端意図は重複扱いしない）
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-9")))
        again = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert again.state is OrderState.ACK

    def test_HTTP失敗も送信済み可能性として突合に回す(self, manager, client) -> None:
        client.send_queue.append(HttpFailure(status=502, code=None, message="bad gateway"))
        client.orders_by_symbol["8306"] = []
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.PLANNED
        assert len(client.sent) == 1

    def test_通信失敗連続でキルスイッチ(self, repo, client, notifier) -> None:
        ks = KillSwitch(max_daily_loss=100, consecutive_failure_threshold=2)
        manager = OrderManager(repo, client, notifier, kill_switch=ks)  # type: ignore[arg-type]
        client.send_queue.append(net_failure())
        client.orders_result = NetworkFailure(kind=NETWORK_KIND_PROTOCOL, message="down")
        # 送信失敗＋突合失敗で連続2回 → この時点でキルスイッチ作動
        manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert ks.active
        assert notifier.subjects(Severity.CRITICAL)
        with pytest.raises(KillSwitchActiveError):
            manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert len(client.sent) == 1  # 作動後は一切送っていない


class TestPoll:
    def _accepted(self, manager, repo, client) -> str:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.order_id
        return intent.intent_id

    def test_WORKINGへの進行(self, manager, repo, client) -> None:
        iid = self._accepted(manager, repo, client)
        client.orders_by_id["OID-1"] = [item(state=3, cum=0)]
        manager.poll(DATE)
        assert repo.get(iid).state is OrderState.WORKING  # type: ignore[union-attr]

    def test_全約定を記録してFILLED(self, manager, repo, client, notifier) -> None:
        iid = self._accepted(manager, repo, client)
        client.orders_by_id["OID-1"] = [item(state=5, cum=100, details=[fill_detail(100)])]
        manager.poll(DATE)
        intent = repo.get(iid)
        assert intent.state is OrderState.FILLED  # type: ignore[union-attr]
        assert len(repo.executions_for(iid)) == 1

        # 2回目のpollでも約定は重複記録されない
        manager.poll(DATE)
        assert len(repo.executions_for(iid)) == 1

    def test_部分約定で残余取消を1回だけ要求(self, manager, repo, client) -> None:
        iid = self._accepted(manager, repo, client)
        client.orders_by_id["OID-1"] = [item(state=3, cum=40, details=[fill_detail(40)])]
        manager.poll(DATE)
        assert repo.get(iid).state is OrderState.PART_FILLED  # type: ignore[union-attr]
        assert client.cancelled == ["OID-1"]
        manager.poll(DATE)  # まだ作業中
        assert client.cancelled == ["OID-1"]  # 2回目の取消要求はしない

    def test_HOLDポリシーでは取消しない(self, repo, client, notifier) -> None:
        manager = OrderManager(repo, client, notifier, partial_fill_policy=PartialFillPolicy.HOLD)  # type: ignore[arg-type]
        self._accepted2(manager, client)
        client.orders_by_id["OID-1"] = [item(state=3, cum=40, details=[fill_detail(40)])]
        manager.poll(DATE)
        assert client.cancelled == []

    def _accepted2(self, manager, client) -> None:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        manager.submit(request(), trade_date=DATE, ref_price=1000)

    def test_部分約定のまま失効終了はALERT(self, manager, repo, client, notifier) -> None:
        iid = self._accepted(manager, repo, client)
        client.orders_by_id["OID-1"] = [
            item(
                state=5,
                cum=40,
                details=[fill_detail(40), {"SeqNum": 3, "RecType": 7, "State": 3}],
            )
        ]
        manager.poll(DATE)
        assert repo.get(iid).state is OrderState.EXPIRED  # type: ignore[union-attr]
        assert len(repo.executions_for(iid)) == 1
        assert notifier.subjects(Severity.ALERT)

    def test_取消終了はCANCELED(self, manager, repo, client) -> None:
        iid = self._accepted(manager, repo, client)
        client.orders_by_id["OID-1"] = [
            item(state=5, cum=0, details=[{"SeqNum": 2, "RecType": 6, "State": 3}])
        ]
        manager.poll(DATE)
        assert repo.get(iid).state is OrderState.CANCELED  # type: ignore[union-attr]

    def test_ブローカーに見つからない場合は状態不変(self, manager, repo, client) -> None:
        iid = self._accepted(manager, repo, client)
        client.orders_by_id["OID-1"] = []
        manager.poll(DATE)
        assert repo.get(iid).state is OrderState.ACK  # type: ignore[union-attr]
        events = [e["event_type"] for e in repo.events_for(iid)]
        assert "POLL_ORDER_MISSING" in events

    def test_SENT意図はpollで自動突合(self, manager, repo, client) -> None:
        """応答喪失でSENT残存後、reconcileが失敗していた分をpollで回収する."""
        client.send_queue.append(net_failure())
        client.orders_result = NetworkFailure(kind=NETWORK_KIND_PROTOCOL, message="down")
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.SENT
        client.orders_result = None
        client.orders_by_symbol["8306"] = [item(state=3)]
        manager.poll(DATE)
        assert repo.get(intent.intent_id).state is OrderState.ACK  # type: ignore[union-attr]


class TestCancel:
    def test_取消要求のイベント記録(self, manager, repo, client) -> None:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert manager.cancel(intent.intent_id, reason="手動") is True
        events = [e["event_type"] for e in repo.events_for(intent.intent_id)]
        assert "CANCEL_REQUESTED" in events

    def test_取消失敗はFalseとイベント(self, manager, repo, client) -> None:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        client.cancel_results.append(
            ApiFailure(code=41, message="指定期明が存在しません", kind="order")
        )
        assert manager.cancel(intent.intent_id) is False
        events = [e["event_type"] for e in repo.events_for(intent.intent_id)]
        assert "CANCEL_FAILED" in events


class TestStartupReconcile:
    def test_オーファン建玉で全停止(self, repo, client, notifier) -> None:
        ks = KillSwitch(max_daily_loss=10**9)
        manager = OrderManager(repo, client, notifier, kill_switch=ks)  # type: ignore[arg-type]
        client.positions_result = ApiSuccess(
            PositionsResponse(
                Holdings=[PositionItem(Symbol="1475", Exchange=1, Side="2", HoldQty=100)]
            )
        )
        report = manager.startup_reconcile(DATE)
        assert len(report.orphan_positions) == 1
        assert notifier.subjects(Severity.CRITICAL)
        with pytest.raises(KillSwitchActiveError):
            manager.submit(request(), trade_date=DATE, ref_price=1000)

    def test_当日FILLED意図で説明できる建玉はオーファンでない(self, manager, repo, client) -> None:
        client.send_queue.append(ApiSuccess(OrderResult(Result=0, OrderId="OID-1")))
        manager.submit(request(), trade_date=DATE, ref_price=1000)
        client.orders_by_id["OID-1"] = [item(state=5, cum=100, details=[fill_detail(100)])]
        manager.poll(DATE)
        client.positions_result = ApiSuccess(
            PositionsResponse(
                Holdings=[PositionItem(Symbol="8306", Exchange=9, Side="2", HoldQty=100)]
            )
        )
        report = manager.startup_reconcile(DATE)
        assert report.orphan_positions == []
        assert report.positions_available

    def test_SENT残りは突合で復旧しALERT対象(self, manager, repo, client) -> None:
        """クラッシュでSENTのまま終了した状況を再現（再起動テスト）."""
        client.send_queue.append(net_failure())
        client.orders_result = NetworkFailure(kind=NETWORK_KIND_PROTOCOL, message="down")
        intent = manager.submit(request(), trade_date=DATE, ref_price=1000)
        assert intent.state is OrderState.SENT

        # 「再起動」を模して新マネージャ（同一DB・同一クライアント復旧後）
        client.orders_result = None
        client.orders_by_symbol["8306"] = [item(state=3)]
        rebooted = OrderManager(repo, client, InMemoryNotifier())  # type: ignore[arg-type]
        report = rebooted.startup_reconcile(DATE)
        states = {i.intent_id: i.state for i in report.open_intents}
        assert states[intent.intent_id] is OrderState.ACK
        assert len(client.sent) == 1  # 再起動でも再送していない
