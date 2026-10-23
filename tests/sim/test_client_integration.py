"""KabuClient × フェイクkabuステーション の統合テスト（実HTTP・ローカルのみ）.

uvicorn でエフェメラルポートにフェイクを起動する。本番 (18080) への
接続は conftest のガードで常時遮断されている。
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from strata.api.client import KabuClient
from strata.api.models import OrderSide, SendOrderRequest
from strata.api.results import (
    NETWORK_KIND_TIMEOUT,
    ApiFailure,
    ApiSuccess,
    NetworkFailure,
    ParseFailure,
)
from strata.config import Settings
from strata.sim.server import SimBroker, create_app

pytestmark = pytest.mark.sim


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class SimServer:
    def __init__(self, broker: SimBroker) -> None:
        self.broker = broker
        self.port = _free_port()
        config = uvicorn.Config(
            create_app(broker), host="127.0.0.1", port=self.port, log_level="error"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:  # pragma: no cover
                raise RuntimeError("フェイクサーバの起動に失敗")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def broker() -> Iterator[SimBroker]:
    broker = SimBroker(api_password="testpass")
    server = SimServer(broker)
    server.start()
    broker._strata_port = server.port  # type: ignore[attr-defined]
    yield broker
    server.stop()


def make_client(broker: SimBroker, tmp_path: Path, **api_kw: object) -> KabuClient:
    settings = Settings(
        api={
            "environment": "test",
            "host": "127.0.0.1",
            "port_override": broker._strata_port,  # type: ignore[attr-defined]
            "password": "testpass",
            "rate_limit_order_per_s": 5.0,
            "rate_limit_info_per_s": 10.0,
            "retry_backoff_base_s": 0.05,
            **api_kw,
        }
    )
    return KabuClient(settings, issuance_lock_path=tmp_path / "token.lock")


class TestFullFlow:
    def test_認証から発注取消までの一気通貫(self, broker: SimBroker, tmp_path: Path) -> None:
        client = make_client(broker, tmp_path)
        assert isinstance(client.authenticate(), ApiSuccess)

        wallet = client.wallet_cash()
        assert isinstance(wallet, ApiSuccess)
        assert wallet.data.Payable == 1_000_000

        assert isinstance(client.register([("8306", 1)]), ApiSuccess)

        order = client.sendorder(SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100))
        assert isinstance(order, ApiSuccess)
        order_id = order.data.OrderId
        assert order_id

        queried = client.orders(order_id=order_id)
        assert isinstance(queried, ApiSuccess)
        assert len(queried.data) == 1
        assert queried.data[0].order_qty == 100

        cancelled = client.cancelorder(str(order_id))
        assert isinstance(cancelled, ApiSuccess)

        after = client.orders(order_id=str(order_id))
        assert isinstance(after, ApiSuccess)
        assert after.data[0].State == 5  # 終了

    def test_リジェクトはコード解決済みで返る(self, broker: SimBroker, tmp_path: Path) -> None:
        broker.scripted_rejects.append(21)
        client = make_client(broker, tmp_path)
        client.authenticate()
        result = client.sendorder(SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100))
        assert isinstance(result, ApiFailure)
        assert result.code == 21
        assert "可能額" in result.message

    def test_パスワード不正の認証失敗(self, broker: SimBroker, tmp_path: Path) -> None:
        client = make_client(broker, tmp_path, password="wrong")
        result = client.authenticate()
        assert isinstance(result, ApiFailure)
        assert result.code == 4001013


class TestFaultInjection:
    def test_応答喪失は再送されず突合で検知できる(self, broker: SimBroker, tmp_path: Path) -> None:
        """P2の突合設計の前提: 応答喪失時にクライアントは1回しか送っていない."""
        client = make_client(broker, tmp_path)
        client.authenticate()
        broker.drop_next_sendorder_response = True

        result = client.sendorder(SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100))
        # 応答喪失の現れ方（接続断 / 途中で切れたJSON）を問わず、
        # 「送信済み可能性あり＝再送禁止」として扱えることが本質
        assert isinstance(result, NetworkFailure | ParseFailure)
        assert result.send_may_have_reached is True

        # サーバには到達済み → /orders 突合で検知（二重発注にならないこと）
        sent_count = broker.request_count.get("POST /kabusapi/sendorder", 0)
        assert sent_count == 1
        queried = client.orders()
        assert isinstance(queried, ApiSuccess)
        assert len(queried.data) == 1

    def test_GETのタイムアウトはリトライ後失敗(self, broker: SimBroker, tmp_path: Path) -> None:
        client = make_client(
            broker,
            tmp_path,
            timeout_read_s=0.3,
            retry_max_attempts=2,
        )
        client.authenticate()
        broker.delay_s = 1.0
        try:
            result = client.wallet_cash()
        finally:
            broker.delay_s = 0.0
        assert isinstance(result, NetworkFailure)
        assert result.kind == NETWORK_KIND_TIMEOUT
        assert result.attempts == 2
        # サーバ側の受信回数がリトライ回数と一致
        assert broker.request_count.get("GET /kabusapi/wallet/cash", 0) == 2

    def test_トークン失効から自動復帰(self, broker: SimBroker, tmp_path: Path) -> None:
        client = make_client(broker, tmp_path)
        client.authenticate()
        # 別プロセスがトークンを再発行した状況を模倣（旧トークン失効）
        broker.token = "issued-by-another-process"

        result = client.wallet_cash()
        # 401 → 自動再認証 → 1回リプレイ で成功するはず
        assert isinstance(result, ApiSuccess)
        assert broker.request_count.get("GET /kabusapi/wallet/cash", 0) == 2

    def test_再認証に失敗したら401で終了し無限リトライしない(
        self, broker: SimBroker, tmp_path: Path
    ) -> None:
        from strata.api.results import HttpFailure

        client = make_client(broker, tmp_path)
        client.authenticate()
        # トークン失効 + 再認証も通らない状況（kabuS側でパスワード変更等）
        broker.token = "issued-by-another-process"
        broker.api_password = "changed-password"

        result = client.wallet_cash()
        assert isinstance(result, HttpFailure)
        assert result.status == 401
        # GET1回 + 再認証1回だけで打ち切り（リプレイは成功時しか行わない）
        assert broker.request_count.get("GET /kabusapi/wallet/cash", 0) == 1
        assert broker.request_count.get("POST /kabusapi/token", 0) == 2
