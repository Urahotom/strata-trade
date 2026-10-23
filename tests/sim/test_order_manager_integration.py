"""OrderManager × フェイクkabuステーション の統合テスト（実HTTP・ローカルのみ）.

応答喪失→突合、クラッシュ復旧、オーファン検知など、
実HTTPを跨がないと見えない挙動に絞る（詳細ロジックはユニットで保証）。
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
import uvicorn

from strata.api.client import KabuClient
from strata.api.models import OrderSide, PriceType, SendOrderRequest
from strata.api.results import ApiSuccess
from strata.config import Settings
from strata.notify.base import InMemoryNotifier, Severity
from strata.order.manager import OrderManager
from strata.order.repository import Intent, IntentRepository
from strata.order.states import OrderState
from strata.risk.killswitch import KillSwitch
from strata.sim.server import SimBroker, create_app
from strata.store import init_db

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
    br = SimBroker(api_password="testpass")
    server = SimServer(br)
    server.start()
    br._strata_port = server.port  # type: ignore[attr-defined]
    yield br
    server.stop()


def make_client(broker: SimBroker, tmp_path: Path) -> KabuClient:
    settings = Settings(
        api={
            "environment": "test",
            "host": "127.0.0.1",
            "port_override": broker._strata_port,  # type: ignore[attr-defined]
            "password": "testpass",
            "rate_limit_order_per_s": 5.0,
            "rate_limit_info_per_s": 10.0,
            "retry_backoff_base_s": 0.05,
        }
    )
    return KabuClient(settings, issuance_lock_path=tmp_path / "token.lock")


def make_request() -> SendOrderRequest:
    return SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100, price_type=PriceType.MARKET)


@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[IntentRepository]:
    conn = init_db(tmp_path / "integration.db")
    yield IntentRepository(conn)
    conn.close()


TODAY = date.today().isoformat()


class TestLifecycleOverHttp:
    def test_発注から約定pollまで実HTTPで通る(
        self, broker: SimBroker, repo: IntentRepository, tmp_path: Path
    ) -> None:
        client = make_client(broker, tmp_path)
        assert isinstance(client.authenticate(), ApiSuccess)
        notifier = InMemoryNotifier()
        manager = OrderManager(repo, client, notifier)

        intent = manager.submit(make_request(), trade_date=TODAY, ref_price=1000)
        assert intent.state is OrderState.ACK
        order_id = intent.order_id
        assert order_id

        # 約定発生（全量約定で終端）
        broker.finalize(order_id, 100, fill_price=1002.0)
        changed = manager.poll(TODAY)
        assert changed is not None and len(changed) == 1
        filled = repo.get(intent.intent_id)
        assert filled is not None and filled.state is OrderState.FILLED
        assert len(repo.executions_for(intent.intent_id)) == 1

        # 2回目のpollは冪等（約定も状態も増えない）
        manager.poll(TODAY)
        assert len(repo.executions_for(intent.intent_id)) == 1

    def test_リジェクトは実コード解決済みで台帳に残る(
        self, broker: SimBroker, repo: IntentRepository, tmp_path: Path
    ) -> None:
        broker.scripted_rejects.append(21)
        client = make_client(broker, tmp_path)
        client.authenticate()
        manager = OrderManager(repo, client, InMemoryNotifier())
        intent = manager.submit(make_request(), trade_date=TODAY, ref_price=1000)
        assert intent.state is OrderState.REJECTED
        assert intent.reject_code == 21
        assert intent.reject_message and "可能額" in intent.reject_message


class TestCrashRecoveryOverHttp:
    def test_応答喪失から突合で復旧し発注は1回だけ(
        self, broker: SimBroker, repo: IntentRepository, tmp_path: Path
    ) -> None:
        client = make_client(broker, tmp_path)
        client.authenticate()
        manager = OrderManager(repo, client, InMemoryNotifier())
        broker.drop_next_sendorder_response = True

        intent = manager.submit(make_request(), trade_date=TODAY, ref_price=1000)
        assert intent.state is OrderState.ACK  # 突合で到達を確認して復旧
        assert broker.request_count.get("POST /kabusapi/sendorder", 0) == 1

    def test_クラッシュでSENT残存から起動時リコンサイルで収束(
        self, broker: SimBroker, repo: IntentRepository, tmp_path: Path
    ) -> None:
        client = make_client(broker, tmp_path)
        client.authenticate()

        # クラッシュ状況の作成: 意図をSENTで書き込み「送信前」で停止
        # （write-ahead 送信済み記録 > 実際の送信、という最悪順序）
        intent = Intent.new(
            trade_date=TODAY,
            symbol="8306",
            market=9,
            side=OrderSide.BUY,
            qty=100,
            price_type=PriceType.MARKET,
        )
        repo.create(intent)
        repo.transition(intent.intent_id, OrderState.SENT, event_type="SENT")

        notifier = InMemoryNotifier()
        rebooted = OrderManager(repo, client, notifier)
        report = rebooted.startup_reconcile(TODAY)
        states = {i.intent_id: i.state for i in report.open_intents}
        # ブローカーに見つからない → 未到着確定で PLANNED 巻き戻し
        assert states[intent.intent_id] is OrderState.PLANNED

        # ここからなら安全に再送してよい（送信はここから1回）
        retried = rebooted.retry(intent.intent_id, ref_price=1000)
        assert retried.state is OrderState.ACK
        assert broker.request_count.get("POST /kabusapi/sendorder", 0) == 1

    def test_オーファン建玉を検知して発注不可(
        self, broker: SimBroker, repo: IntentRepository, tmp_path: Path
    ) -> None:
        broker.holdings.append(
            {"Symbol": "1475", "Exchange": 1, "Side": "2", "HoldQty": 100, "Price": 1000}
        )
        client = make_client(broker, tmp_path)
        client.authenticate()
        ks = KillSwitch(max_daily_loss=10**9)
        notifier = InMemoryNotifier()
        manager = OrderManager(repo, client, notifier, kill_switch=ks)

        report = manager.startup_reconcile(TODAY)
        assert len(report.orphan_positions) == 1
        assert ks.active
        assert notifier.subjects(Severity.CRITICAL)

        from strata.order.manager import KillSwitchActiveError

        with pytest.raises(KillSwitchActiveError):
            manager.submit(make_request(), trade_date=TODAY, ref_price=1000)
        assert broker.request_count.get("POST /kabusapi/sendorder", 0) == 0
