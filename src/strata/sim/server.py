"""フェイクkabuステーションAPIサーバ.

公式仕様（reference/kabu_STATION_API.yaml / ptal/error.html）に寄せた
振る舞いを模した開発用スタブ。

再現する仕様:
- POST /token: 新トークン発行で旧トークンは失効
- X-API-KEY 不正・未発行: HTTP 401 {"Code": 4001009}
- POST /sendorder: 受理時 {"Result": 0, "OrderId": "..."} / リジェクト時 Result != 0
- GET /orders: 注文状態（state 1〜5）と Details.RecType（8=約定）
- PUT /cancelorder: 該当注文なしは {"Result": 4004001系} 相当
- シナリオ注入: 発注応答の喪失（接続クローズ）、発注リジェクト、遅延

P0では骨格（発注→照会→取消の最小フローと認証）のみ実装し、
部分約定・レート制限・遅延注入などは P1/P2 で拡張する。
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

#: 発注必須パラメータ（RequestSendOrder より）
REQUIRED_SENDORDER_FIELDS = (
    "Symbol",
    "Exchange",
    "SecurityType",
    "Side",
    "CashMargin",
    "DelivType",
    "AccountType",
    "Qty",
    "Price",
    "ExpireDay",
    "FrontOrderType",
)

#: 発注パラメータ不正系のコード（4002xxx）
CODE_PARAM_MISSING = 4002001
#: APIキー不一致
CODE_API_KEY_MISMATCH = 4001009
#: トークン取得失敗（パスワード不正）
CODE_TOKEN_FAILED = 4001013
#: 該当注文なし（取消時）
RESULT_ORDER_NOT_FOUND = 41


def _jst_compact(now: datetime | None = None) -> str:
    """JSTの yyyyMMddHHmmss（公式 RecvTime 形式）を返す."""
    from datetime import timedelta

    dt = now or datetime.now(UTC)
    return (dt + timedelta(hours=9)).strftime("%Y%m%d%H%M%S")


@dataclass
class SimOrder:
    """フェイカの内部注文台帳."""

    order_id: str
    symbol: str
    exchange: int
    side: str
    qty: int
    price: float
    front_order_type: int
    state: int = 3  # 3=処理済(発注済)
    recv_time: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.recv_time:
            self.recv_time = _jst_compact()

    def to_dict(self, with_details: bool = True) -> dict[str, Any]:
        data = {
            "ID": self.order_id,
            "State": self.state,
            "OrderState": _STATE_LABEL.get(self.state, ""),
            "OrdType": 1,
            "RecvTime": self.recv_time,
            "Symbol": self.symbol,
            "SymbolName": self.symbol,
            "Exchange": self.exchange,
            "ExchangeName": "東証",
            "Price": self.price,
            "OrderQty": self.qty,
            "CumQty": sum(d["Qty"] for d in self.details if d["RecType"] == 8),
            "Side": self.side,
            "CashMargin": 1,
            "AccountType": 4,
            "DelivType": 2,
            "ExpireDay": 0,
        }
        if with_details:
            data["Details"] = self.details
        return data


@dataclass
class SimBroker:
    """フェイカの共有状態.

    テストから直接書き換えてシナリオを制御する。
    """

    api_password: str = "testpass"
    #: 有効なトークン（新発行で旧は失効させるため1つのみ維持）
    token: str | None = None
    orders: dict[str, SimOrder] = field(default_factory=dict)
    #: 次の発注で返す Result コード列（空なら常に成功）。枯渇後は成功
    scripted_rejects: list[int] = field(default_factory=list)
    #: True で次の発注の応答を喪失させる（接続クローズ）
    drop_next_sendorder_response: bool = False
    #: 全エンドポイントの遅延（秒）。タイムアウトテスト用
    delay_s: float = 0.0
    #: 銘柄登録状態 {「symbol@market」: market}
    registered: dict[str, int] = field(default_factory=dict)
    #: GET /positions で返す建玉（テストから組み立てる）
    holdings: list[dict[str, Any]] = field(default_factory=list)
    _order_seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _token_seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    request_count: dict[str, int] = field(default_factory=dict)

    def next_order_id(self) -> str:
        day = datetime.now(UTC).strftime("%Y%m%d")
        return f"{day}A01N{next(self._order_seq):010d}"

    def finalize(
        self,
        order_id: str,
        filled_qty: int,
        *,
        mode: str = "expire",
        fill_price: float | None = None,
    ) -> None:
        """注文を台帳上で終端化する（部分約定・失効・取消の再現）.

        Args:
            filled_qty: 約定数量（0以上注文数量以下）
            mode: 残余の終端理由 "expire"（失効 RecType=7）/ "cancel"（取消 6）
            fill_price: 約定価格（未指定なら注文価格、成行は1000円扱い）
        """
        order = self.orders[order_id]
        if not 0 <= filled_qty <= order.qty:
            raise ValueError(f"約定数量が不正です: {filled_qty}/{order.qty}")
        if filled_qty > 0:
            order.details.append(
                {
                    "SeqNum": len(order.details) + 1,
                    "RecType": 8,
                    "State": 3,
                    "Price": fill_price if fill_price is not None else (order.price or 1000.0),
                    "Qty": filled_qty,
                    "ExecutionID": f"X-{order.order_id}",
                    "TransactTime": _jst_compact(),
                }
            )
        remaining = order.qty - filled_qty
        if remaining > 0:
            rec_type = 6 if mode == "cancel" else 7
            order.details.append(
                {"SeqNum": len(order.details) + 1, "RecType": rec_type, "State": 3}
            )
        order.state = 5


_STATE_LABEL = {
    1: "待機",
    2: "処理中",
    3: "処理済",
    4: "訂正取消送信中",
    5: "終了",
}


class ConnectionDroppedError(Exception):
    """応答喪失のシミュレーション（ASGI例外で接続を落とす）."""


def _auth_dependency(broker: SimBroker) -> Callable[[Request], None]:
    def _auth(request: Request) -> None:
        key = request.headers.get("X-API-KEY")
        if broker.token is None or key != broker.token:
            raise _HttpApiError(
                401,
                {
                    "ResultCode": 1,
                    "Code": CODE_API_KEY_MISMATCH,
                    "Message": "APIキーが一致しません",
                },
            )

    return _auth


class _HttpApiError(Exception):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(str(payload))
        self.status_code = status_code
        self.payload = payload


def create_app(broker: SimBroker | None = None) -> FastAPI:
    """フェイクkabuステーションアプリを生成する."""
    b = broker or SimBroker()
    app = FastAPI(title="strata kabu simulator", docs_url=None, redoc_url=None)
    app.state.broker = b

    @app.exception_handler(_HttpApiError)
    async def _handle_api_error(request: Request, exc: _HttpApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload)

    @app.middleware("http")
    async def _count_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        key = f"{request.method} {request.url.path}"
        b.request_count[key] = b.request_count.get(key, 0) + 1
        if b.delay_s > 0:
            await asyncio.sleep(b.delay_s)
        return await call_next(request)

    # ---------------- auth ----------------

    @app.post("/kabusapi/token")
    async def token(body: dict[str, Any]) -> dict[str, Any]:
        b.request_count["token"] = b.request_count.get("token", 0) + 1
        if body.get("APIPassword") != b.api_password:
            return {"ResultCode": CODE_TOKEN_FAILED, "Token": ""}
        # 仕様: 別トークン発行で旧トークンは失効
        b.token = f"sim-token-{next(b._token_seq):08d}"
        return {"ResultCode": 0, "Token": b.token}

    auth = _auth_dependency(b)

    # ---------------- order ----------------

    @app.post("/kabusapi/sendorder", dependencies=[Depends(auth)])
    async def sendorder(request: Request) -> Response:
        # 応答喪失のフラグは先に回収する（注文処理自体は成功させる）
        drop_response = b.drop_next_sendorder_response
        b.drop_next_sendorder_response = False
        body = await request.json()
        missing = [f for f in REQUIRED_SENDORDER_FIELDS if f not in body]
        if missing:
            return JSONResponse(
                status_code=400,
                content={
                    "ResultCode": 1,
                    "Code": CODE_PARAM_MISSING,
                    "Message": f"パラメータがありません: {missing[0]}",
                },
            )
        if b.scripted_rejects:
            result_code = b.scripted_rejects.pop(0)
            return JSONResponse(content={"Result": result_code, "OrderId": ""})
        order_id = b.next_order_id()
        order = SimOrder(
            order_id=order_id,
            symbol=str(body["Symbol"]),
            exchange=int(body["Exchange"]),
            side=str(body["Side"]),
            qty=int(body["Qty"]),
            price=float(body["Price"]),
            front_order_type=int(body["FrontOrderType"]),
            details=[{"SeqNum": 1, "RecType": 4, "State": 3, "Qty": int(body["Qty"])}],
        )
        b.orders[order_id] = order
        if drop_response:
            # 応答ヘッダを送り始めた直後に接続を落とす（=応答喪失の現実的再現）。
            # サーバ側には注文が届いているため、突合 (/orders) で検知できる。
            def lossy_body() -> Iterator[bytes]:
                yield b'{"Result": 0, "Ord'
                raise ConnectionDroppedError("simulated response loss")

            return StreamingResponse(lossy_body(), media_type="application/json")
        return JSONResponse(content={"Result": 0, "OrderId": order_id})

    @app.put("/kabusapi/cancelorder", dependencies=[Depends(auth)])
    async def cancelorder(body: dict[str, Any]) -> dict[str, Any]:
        order_id = str(body.get("OrderId") or body.get("OrderID") or "")
        order = b.orders.get(order_id)
        if order is None or order.state == 5:
            return {"Result": RESULT_ORDER_NOT_FOUND, "OrderId": ""}
        order.state = 5
        order.details.append({"SeqNum": len(order.details) + 1, "RecType": 6, "State": 3})
        return {"Result": 0, "OrderId": order_id}

    # ---------------- info ----------------

    @app.get("/kabusapi/orders", dependencies=[Depends(auth)])
    async def orders(
        id: str | None = None,
        symbol: str | None = None,
        state: int | None = None,
        side: str | None = None,
        details: bool = True,
    ) -> list[dict[str, Any]]:
        result = list(b.orders.values())
        if id:
            result = [o for o in result if o.order_id.lower() == id.lower()]
        if symbol:
            result = [o for o in result if o.symbol == symbol.split("@")[0]]
        if state is not None:
            result = [o for o in result if o.state == state]
        if side is not None:
            result = [o for o in result if o.side == side]
        return [o.to_dict(with_details=details) for o in result]

    @app.get("/kabusapi/positions", dependencies=[Depends(auth)])
    async def positions() -> dict[str, Any]:
        return {"PushButton": None, "Holdings": b.holdings, "TotalProfitLoss": 0}

    @app.get("/kabusapi/wallet/cash", dependencies=[Depends(auth)])
    async def wallet_cash() -> dict[str, Any]:
        return {
            "Deposit": 0,
            "GuaranteeMoney": 1_000_000,
            "LifeMargin": 1_000_000,
            "MarginBalance": 1_000_000,
            "MarginOpenProfit": 0,
            "MarginTotalBalance": 1_000_000,
            "MarginUsable": 1_000_000,
            "NDaySettleCallMargin": 0,
            "Payable": 1_000_000,
            "Repayment": 0,
            "TotalAmountMarginTradingAccount": 0,
            "TradeQuantity": 0,
        }

    @app.get("/kabusapi/board/{symbol}", dependencies=[Depends(auth)])
    async def board(symbol: str) -> dict[str, Any]:
        return {"Symbol": symbol.split("@")[0], "CurrentPrice": None}

    # ---------------- register ----------------

    @app.put("/kabusapi/register", dependencies=[Depends(auth)])
    async def register(body: dict[str, Any]) -> Response:
        symbols = body.get("Symbols", [])
        if len(b.registered) + len(symbols) > 50:
            return JSONResponse(
                status_code=400,
                content={"ResultCode": 1, "Code": 4001018, "Message": "銘柄登録上限(50)"},
            )
        for item in symbols:
            key = f"{item['Symbol']}@{item['Exchange']}"
            b.registered[key] = int(item["Exchange"])
        return JSONResponse(
            content={
                "RegistList": [
                    {"Symbol": k.split("@")[0], "Exchange": v} for k, v in b.registered.items()
                ]
            }
        )

    @app.put("/kabusapi/unregister/all", dependencies=[Depends(auth)])
    async def unregister_all() -> dict[str, Any]:
        b.registered.clear()
        return {"RegistList": []}

    # ---------------- シミュレータ制御（開発専用・公式仕様外） ----------------
    # 手動検証用にシナリオをその場で注入するための namespace。
    # 実運用クライアントから呼ぶことは決してない。認証不要（ローカル開発ツール）。

    @app.get("/kabusapi/_sim/state")
    async def sim_state() -> dict[str, Any]:
        return {
            "token_issued": b.token is not None,
            "orders": [o.to_dict() for o in b.orders.values()],
            "holdings": b.holdings,
            "registered": sorted(b.registered),
            "pending_rejects": list(b.scripted_rejects),
            "drop_next_sendorder": b.drop_next_sendorder_response,
            "delay_s": b.delay_s,
            "request_count": dict(b.request_count),
        }

    @app.post("/kabusapi/_sim/reject")
    async def sim_reject(body: dict[str, Any]) -> dict[str, Any]:
        """次の発注から順に返すリジェクトコードを1本積む（既定 21=買付可能額不足）."""
        b.scripted_rejects.append(int(body.get("code", 21)))
        return {"pending_rejects": list(b.scripted_rejects)}

    @app.post("/kabusapi/_sim/drop_next")
    async def sim_drop_next() -> dict[str, bool]:
        """次の発注の応答を喪失させる（到達は済み応答のみ失われる）."""
        b.drop_next_sendorder_response = True
        return {"drop_next_sendorder": True}

    @app.post("/kabusapi/_sim/delay")
    async def sim_delay(body: dict[str, Any]) -> dict[str, float]:
        """全エンドポイントの遅延秒数を設定（0 で解除）."""
        b.delay_s = float(body.get("seconds", 0.0))
        return {"delay_s": b.delay_s}

    @app.post("/kabusapi/_sim/finalize")
    async def sim_finalize(body: dict[str, Any]):  # type: ignore[no-untyped-def]
        """注文を約定させて終端化する（部分約定・失効・取消の再現）."""
        order_id = str(body.get("order_id", ""))
        if order_id not in b.orders:
            return JSONResponse(
                status_code=404, content={"error": f"注文が存在しません: {order_id}"}
            )
        price = body.get("price")
        b.finalize(
            order_id,
            int(body.get("qty", 0)),
            mode=str(body.get("mode", "expire")),
            fill_price=float(price) if price is not None else None,
        )
        return b.orders[order_id].to_dict()

    @app.post("/kabusapi/_sim/holdings")
    async def sim_holdings(body: dict[str, Any]) -> dict[str, Any]:
        """GET /positions で返す建玉を組み立てる（オーファン検知テスト等）."""
        b.holdings = list(body.get("holdings", []))
        return {"holdings": b.holdings}

    return app
