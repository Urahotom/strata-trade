"""KabuClient のユニットテスト（MockTransport による障害注入・完全オフライン）."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from strata.api.client import KabuClient
from strata.api.models import (
    OrderResult,
    OrderSide,
    PriceType,
    SendOrderRequest,
    TokenResult,
    WalletCash,
)
from strata.api.results import (
    NETWORK_KIND_CONNECT,
    NETWORK_KIND_PROTOCOL,
    NETWORK_KIND_TIMEOUT,
    ApiFailure,
    ApiSuccess,
    HttpFailure,
    NetworkFailure,
    ParseFailure,
)
from strata.config import Settings


class ScriptedTransport:
    """リクエスト順にスクリプトされた応答/例外を返すトランスポート."""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("スクリプト以上のリクエストが発生しました")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, httpx.Response)
        return item

    def count(self, method: str, path_contains: str) -> int:
        return sum(1 for r in self.requests if r.method == method and path_contains in r.url.path)


def make_settings(**api_overrides: object) -> Settings:
    api: dict[str, object] = {
        "environment": "test",
        "password": "testpass",
        "retry_backoff_base_s": 0.5,
    }
    api.update(api_overrides)
    return Settings(api=api)  # type: ignore[arg-type]


def make_client(
    script: list[object],
    tmp_path: Path,
    sleeps: list[float],
    **api_overrides: object,
) -> tuple[KabuClient, ScriptedTransport]:
    transport = ScriptedTransport(script)
    settings = make_settings(**api_overrides)
    client = KabuClient(
        settings,
        transport=httpx.MockTransport(transport.handler),
        issuance_lock_path=tmp_path / "token.lock",
        sleep=sleeps.append,
    )
    return client, transport


def json_resp(status: int = 200, data: object = None) -> httpx.Response:
    return httpx.Response(status, json=data)


ORDER_REQ = SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100)


class TestAuthenticate:
    def test_认证成功でトークン保持と以後のリクエストに付与(self, tmp_path: Path) -> None:
        sleeps: list[float] = []
        client, t = make_client(
            [json_resp(data={"ResultCode": 0, "Token": "tok-1"}), json_resp(data={"Payable": 100})],
            tmp_path,
            sleeps,
        )
        result = client.authenticate()
        assert isinstance(result, ApiSuccess)
        assert client.has_token
        client.wallet_cash()
        assert t.requests[1].headers["X-API-KEY"] == "tok-1"

    def test_パスワード不正はApiFailure(self, tmp_path: Path) -> None:
        client, _ = make_client(
            [json_resp(data={"ResultCode": 4001013, "Token": ""})], tmp_path, []
        )
        result = client.authenticate()
        assert isinstance(result, ApiFailure)
        assert result.code == 4001013
        assert result.kind == "token"
        assert not client.has_token


class TestGetRetry:
    def test_タイムアウト後に再試行して成功(self, tmp_path: Path) -> None:
        sleeps: list[float] = []
        client, t = make_client(
            [
                httpx.ConnectTimeout("t/o"),
                httpx.ReadTimeout("t/o"),
                json_resp(data={"Payable": 1}),
            ],
            tmp_path,
            sleeps,
        )
        result = client.wallet_cash()
        assert isinstance(result, ApiSuccess)
        assert isinstance(result.data, WalletCash)
        assert t.count("GET", "wallet/cash") == 3
        assert sleeps == [0.5, 1.0]  # 指数バックオフ

    def test_タイムアウト継続なら3回でNetworkFailure(self, tmp_path: Path) -> None:
        sleeps: list[float] = []
        client, t = make_client([httpx.ReadTimeout("t/o")] * 3, tmp_path, sleeps)
        result = client.wallet_cash()
        assert isinstance(result, NetworkFailure)
        assert result.kind == NETWORK_KIND_TIMEOUT
        assert result.attempts == 3
        assert not result.send_may_have_reached
        assert t.count("GET", "wallet/cash") == 3

    def test_接続断はconnect分類(self, tmp_path: Path) -> None:
        client, _ = make_client([httpx.ConnectError("refused")] * 3, tmp_path, [])
        result = client.wallet_cash()
        assert isinstance(result, NetworkFailure)
        assert result.kind == NETWORK_KIND_CONNECT

    def test_429はバックオフして再試行(self, tmp_path: Path) -> None:
        sleeps: list[float] = []
        client, t = make_client(
            [
                json_resp(429, {"Code": 4001006, "Message": "API実行回数エラー"}),
                json_resp(data={"Payable": 2}),
            ],
            tmp_path,
            sleeps,
        )
        result = client.wallet_cash()
        assert isinstance(result, ApiSuccess)
        assert t.count("GET", "wallet/cash") == 2
        assert sleeps == [0.5]

    def test_429継続ならHttpFailure(self, tmp_path: Path) -> None:
        client, t = make_client(
            [json_resp(429, {"Code": 4001006, "Message": "rate"})] * 3, tmp_path, []
        )
        result = client.wallet_cash()
        assert isinstance(result, HttpFailure)
        assert result.status == 429
        assert result.is_rate_limited
        assert t.count("GET", "wallet/cash") == 3


class TestAuthExpiry:
    def test_GETの401は再認証して1回だけリプレイ(self, tmp_path: Path) -> None:
        client, t = make_client(
            [
                json_resp(401, {"Code": 4001009, "Message": "APIキーが一致しません"}),
                json_resp(data={"ResultCode": 0, "Token": "tok-2"}),  # 再認証
                json_resp(data={"Payable": 3}),  # リプレイ
            ],
            tmp_path,
            [],
        )
        result = client.wallet_cash()
        assert isinstance(result, ApiSuccess)
        assert t.count("POST", "token") == 1
        assert t.count("GET", "wallet/cash") == 2
        assert t.requests[-1].headers["X-API-KEY"] == "tok-2"

    def test_再認証後も401なら失敗(self, tmp_path: Path) -> None:
        client, t = make_client(
            [
                json_resp(401, {"Code": 4001009}),
                json_resp(data={"ResultCode": 0, "Token": "tok-2"}),
                json_resp(401, {"Code": 4001009}),
            ],
            tmp_path,
            [],
        )
        result = client.wallet_cash()
        assert isinstance(result, HttpFailure)
        assert result.is_auth_error
        assert t.count("GET", "wallet/cash") == 2  # リプレイは1回まで

    def test_発注の401は再認証するがリプレイしない(self, tmp_path: Path) -> None:
        """発注の自動再送は二重発注事故に直結するため禁止（設計要否）."""
        client, t = make_client(
            [
                json_resp(401, {"Code": 4001009}),
                json_resp(data={"ResultCode": 0, "Token": "tok-2"}),
            ],
            tmp_path,
            [],
        )
        result = client.sendorder(ORDER_REQ)
        assert isinstance(result, HttpFailure)
        assert result.status == 401
        assert t.count("POST", "sendorder") == 1  # 絶対に1回しか送っていない
        assert t.count("POST", "token") == 1  # トークンは更新済み


class TestSendorder:
    def test_発注成功(self, tmp_path: Path) -> None:
        client, t = make_client([json_resp(data={"Result": 0, "OrderId": "OID-1"})], tmp_path, [])
        result = client.sendorder(ORDER_REQ)
        assert isinstance(result, ApiSuccess)
        assert isinstance(result.data, OrderResult)
        assert result.data.OrderId == "OID-1"
        sent = t.requests[0]
        assert sent.url.path.endswith("/kabusapi/sendorder")

    def test_発注のタイムアウトは送信済み可能性つきで即終了(self, tmp_path: Path) -> None:
        client, t = make_client([httpx.ReadTimeout("t/o")], tmp_path, [])
        result = client.sendorder(ORDER_REQ)
        assert isinstance(result, NetworkFailure)
        assert result.kind == NETWORK_KIND_TIMEOUT
        assert result.send_may_have_reached is True
        assert result.attempts == 1
        assert t.count("POST", "sendorder") == 1  # 自動リトライなし

    def test_発注の切断はprotocol分類で送信済み可能性(self, tmp_path: Path) -> None:
        client, t = make_client([httpx.RemoteProtocolError("closed")], tmp_path, [])
        result = client.sendorder(ORDER_REQ)
        assert isinstance(result, NetworkFailure)
        assert result.kind == NETWORK_KIND_PROTOCOL
        assert result.send_may_have_reached is True
        assert t.count("POST", "sendorder") == 1

    def test_Resultが非0ならリジェクトとしてコード解決(self, tmp_path: Path) -> None:
        client, _ = make_client([json_resp(data={"Result": 21, "OrderId": ""})], tmp_path, [])
        result = client.sendorder(ORDER_REQ)
        assert isinstance(result, ApiFailure)
        assert result.code == 21
        assert result.is_rejection
        assert "可能額" in result.message

    def test_取消も発注系でリトライなし(self, tmp_path: Path) -> None:
        client, t = make_client([httpx.ReadTimeout("t/o")], tmp_path, [])
        result = client.cancelorder("OID-1")
        assert isinstance(result, NetworkFailure)
        assert result.send_may_have_reached is True
        assert t.count("PUT", "cancelorder") == 1


class TestParsing:
    def test_不正JSONはParseFailure(self, tmp_path: Path) -> None:
        client, _ = make_client([httpx.Response(200, text="<html>not json</html>")], tmp_path, [])
        result = client.wallet_cash()
        assert isinstance(result, ParseFailure)

    def test_未知フィールドは無視して続行(self, tmp_path: Path) -> None:
        client, _ = make_client([json_resp(data={"Payable": 9, "FutureFieldXYZ": 1})], tmp_path, [])
        result = client.wallet_cash()
        assert isinstance(result, ApiSuccess)
        assert result.data.Payable == 9

    def test_板情報の取得(self, tmp_path: Path) -> None:
        client, t = make_client(
            [json_resp(data={"Symbol": "8306", "CurrentPrice": 1234.0})], tmp_path, []
        )
        result = client.board("8306", 1)
        assert isinstance(result, ApiSuccess)
        assert result.data.CurrentPrice == 1234.0
        assert "/board/8306@1" in t.requests[0].url.path

    def test_注文照会と約定明細の判定(self, tmp_path: Path) -> None:
        order = {
            "ID": "OID-9",
            "State": 5,
            "Symbol": "8306",
            "Side": "2",
            "OrderQty": 100,
            "CumQty": 60,
            "Details": [
                {"SeqNum": 1, "RecType": 4, "Qty": 100},
                {"SeqNum": 2, "RecType": 8, "Qty": 60, "ExecutionID": "E-1"},
            ],
        }
        client, t = make_client([json_resp(data=[order])], tmp_path, [])
        result = client.orders(symbol="8306", state=5)
        assert isinstance(result, ApiSuccess)
        item = result.data[0]
        assert item.is_terminal
        assert not item.fully_filled  # 部分約定のまま終了
        assert [d.Qty for d in item.fills()] == [60]
        assert t.requests[0].url.params["symbol"] == "8306"
        assert t.requests[0].url.params["state"] == "5"

    def test_建玉照会(self, tmp_path: Path) -> None:
        client, _ = make_client(
            [json_resp(data={"Holdings": [{"Symbol": "8306", "HoldQty": 100}]})],
            tmp_path,
            [],
        )
        result = client.positions()
        assert isinstance(result, ApiSuccess)
        assert result.data.Holdings[0].HoldQty == 100

    def test_銘柄登録ペイロード(self, tmp_path: Path) -> None:
        client, t = make_client([json_resp(data={"RegistList": []})], tmp_path, [])
        result = client.register([("8306", 1), ("9433", 1)])
        assert isinstance(result, ApiSuccess)
        import json

        body = json.loads(t.requests[0].content)
        assert body == {
            "Symbols": [{"Symbol": "8306", "Exchange": 1}, {"Symbol": "9433", "Exchange": 1}]
        }


class TestTokenEndpoint:
    def test_認証はトークン不要で送信(self, tmp_path: Path) -> None:
        client, t = make_client([json_resp(data={"ResultCode": 0, "Token": "tok"})], tmp_path, [])
        client.authenticate()
        assert "X-API-KEY" not in t.requests[0].headers


def test_sendorderリクエストは要検証() -> None:
    # 発注前に pydantic で検証済みであること（防御の第一層）
    with pytest.raises(ValueError):
        SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=0)
    with pytest.raises(ValueError):
        SendOrderRequest(
            symbol="8306", side=OrderSide.BUY, qty=100, price_type=PriceType.MARKET, price=100
        )
    ok = SendOrderRequest(
        symbol="8306", side=OrderSide.SELL, qty=100, price_type=PriceType.LIMIT, price=100
    )
    payload = ok.to_payload()
    assert payload["Side"] == "1"
    assert payload["FundType"] == "  "
    assert TokenResult(ResultCode=0).Token == ""
