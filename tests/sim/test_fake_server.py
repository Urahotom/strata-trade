"""フェイクkabuステーションサーバのテスト（in-process ASGI。ネットワーク不要）."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from strata.sim.server import ConnectionDroppedError, SimBroker, create_app


@pytest.fixture()
def broker() -> SimBroker:
    return SimBroker(api_password="testpass")


@pytest.fixture()
def client(broker: SimBroker) -> TestClient:
    return TestClient(create_app(broker))


def _token(client: TestClient) -> str:
    res = client.post("/kabusapi/token", json={"APIPassword": "testpass"})
    assert res.status_code == 200
    body = res.json()
    assert body["ResultCode"] == 0
    return str(body["Token"])


def _auth(token: str) -> dict[str, str]:
    return {"X-API-KEY": token}


ORDER_BODY = {
    "Symbol": "8306",
    "Exchange": 9,  # SOR
    "SecurityType": 1,
    "Side": "2",
    "CashMargin": 1,
    "DelivType": 2,
    "AccountType": 4,
    "Qty": 100,
    "Price": 0,
    "ExpireDay": 0,
    "FrontOrderType": 10,
}


class TestToken:
    def test_正常発行(self, client: TestClient) -> None:
        token = _token(client)
        assert token

    def test_パスワード不正なら4001013(self, client: TestClient) -> None:
        res = client.post("/kabusapi/token", json={"APIPassword": "wrong"})
        assert res.status_code == 200
        assert res.json()["ResultCode"] == 4001013

    def test_新トークン発行で旧トークン失効(self, client: TestClient) -> None:
        old = _token(client)
        new = _token(client)
        assert old != new
        res = client.get("/kabusapi/orders", headers=_auth(old))
        assert res.status_code == 401
        assert res.json()["Code"] == 4001009
        assert client.get("/kabusapi/orders", headers=_auth(new)).status_code == 200


class TestAuth:
    def test_APIキーなしは401(self, client: TestClient) -> None:
        res = client.get("/kabusapi/orders")
        assert res.status_code == 401

    def test_トークン未取得なら401(self, client: TestClient) -> None:
        res = client.get("/kabusapi/orders", headers=_auth("any"))
        assert res.status_code == 401


class TestOrderFlow:
    def test_発注から照会まで(self, client: TestClient) -> None:
        token = _token(client)
        res = client.post("/kabusapi/sendorder", json=ORDER_BODY, headers=_auth(token))
        assert res.status_code == 200
        body = res.json()
        assert body["Result"] == 0
        order_id = body["OrderId"]
        assert order_id

        res = client.get(f"/kabusapi/orders?id={order_id}", headers=_auth(token))
        assert res.status_code == 200
        orders = res.json()
        assert len(orders) == 1
        assert orders[0]["ID"] == order_id
        assert orders[0]["OrderQty"] == 100
        assert orders[0]["Details"][0]["RecType"] == 4  # 発注

    def test_必須パラメータ欠きは400(self, client: TestClient) -> None:
        token = _token(client)
        bad = dict(ORDER_BODY)
        del bad["Qty"]
        res = client.post("/kabusapi/sendorder", json=bad, headers=_auth(token))
        assert res.status_code == 400
        assert res.json()["Code"] == 4002001

    def test_スクリプト済みリジェクトを返す(self, client: TestClient, broker: SimBroker) -> None:
        token = _token(client)
        broker.scripted_rejects.append(21)  # 可能額不足
        res = client.post("/kabusapi/sendorder", json=ORDER_BODY, headers=_auth(token))
        assert res.status_code == 200
        assert res.json()["Result"] == 21
        # リジェクトは台帳に残らない
        assert client.get("/kabusapi/orders", headers=_auth(token)).json() == []

    def test_注文取消(self, client: TestClient) -> None:
        token = _token(client)
        order_id = client.post("/kabusapi/sendorder", json=ORDER_BODY, headers=_auth(token)).json()[
            "OrderId"
        ]
        res = client.put("/kabusapi/cancelorder", json={"OrderId": order_id}, headers=_auth(token))
        assert res.status_code == 200
        assert res.json()["Result"] == 0
        orders = client.get(f"/kabusapi/orders?id={order_id}", headers=_auth(token)).json()
        assert orders[0]["State"] == 5  # 終了
        assert orders[0]["Details"][-1]["RecType"] == 6  # 取消

    def test_存在しない注文の取消(self, client: TestClient) -> None:
        token = _token(client)
        res = client.put("/kabusapi/cancelorder", json={"OrderId": "NOPE"}, headers=_auth(token))
        assert res.status_code == 200
        assert res.json()["Result"] != 0

    def test_応答喪失はクライアントで例外になる(
        self, client: TestClient, broker: SimBroker
    ) -> None:
        """送信成功・応答喪失の再現。再送前の突合設計 (P2) の検証対象."""
        token = _token(client)
        broker.drop_next_sendorder_response = True
        with pytest.raises(ConnectionDroppedError):
            client.post("/kabusapi/sendorder", json=ORDER_BODY, headers=_auth(token))
        # サーバ側には届いている → 突合で検知できる
        orders = client.get("/kabusapi/orders", headers=_auth(token)).json()
        assert len(orders) == 1


class TestSimControl:
    """単独起動時に対話的にシナリオを注入する制御API（公式仕様外）."""

    def test_stateダンプ(self, client: TestClient) -> None:
        res = client.get("/kabusapi/_sim/state")
        assert res.status_code == 200
        assert res.json()["orders"] == []
        assert res.json()["token_issued"] is False

    def test_reject注入で次発注がリジェクト(self, client: TestClient) -> None:
        client.post("/kabusapi/_sim/reject", json={"code": 21})
        token = _token(client)
        res = client.post("/kabusapi/sendorder", json=ORDER_BODY, headers=_auth(token))
        assert res.json()["Result"] == 21

    def test_finalizeで約定終端(self, client: TestClient) -> None:
        token = _token(client)
        oid = client.post("/kabusapi/sendorder", json=ORDER_BODY, headers=_auth(token)).json()[
            "OrderId"
        ]
        res = client.post(
            "/kabusapi/_sim/finalize",
            json={"order_id": oid, "qty": 100, "mode": "expire", "price": 1000},
        )
        assert res.status_code == 200
        assert res.json()["CumQty"] == 100
        assert res.json()["State"] == 5

    def test_finalizeの注文なしは404(self, client: TestClient) -> None:
        res = client.post("/kabusapi/_sim/finalize", json={"order_id": "NOPE", "qty": 1})
        assert res.status_code == 404

    def test_drop_nextで応答喪失(self, client: TestClient) -> None:
        token = _token(client)
        client.post("/kabusapi/_sim/drop_next")
        with pytest.raises(ConnectionDroppedError):
            client.post("/kabusapi/sendorder", json=ORDER_BODY, headers=_auth(token))

    def test_holdings設定がpositionsに反映(self, client: TestClient) -> None:
        client.post(
            "/kabusapi/_sim/holdings",
            json={"holdings": [{"Symbol": "8306", "Exchange": 9, "Side": "2", "HoldQty": 100}]},
        )
        token = _token(client)
        res = client.get("/kabusapi/positions", headers=_auth(token))
        assert len(res.json()["Holdings"]) == 1

    def test_delay設定(self, client: TestClient, broker: SimBroker) -> None:
        client.post("/kabusapi/_sim/delay", json={"seconds": 0.01})
        assert broker.delay_s == 0.01


class TestLauncher:
    def test_buildでアプリが作れる(self) -> None:
        from strata.sim.__main__ import build

        broker, app = build("127.0.0.1", 18081, "pw")
        assert broker.api_password == "pw"
        res = TestClient(app).post("/kabusapi/token", json={"APIPassword": "pw"})
        assert res.json()["ResultCode"] == 0


class TestInfoStubs:
    def test_wallet_cash(self, client: TestClient) -> None:
        token = _token(client)
        res = client.get("/kabusapi/wallet/cash", headers=_auth(token))
        assert res.status_code == 200
        assert res.json()["Payable"] == 1_000_000

    def test_positions_空(self, client: TestClient) -> None:
        token = _token(client)
        res = client.get("/kabusapi/positions", headers=_auth(token))
        assert res.status_code == 200
        assert res.json()["Holdings"] == []
