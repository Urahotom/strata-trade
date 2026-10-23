"""KabuClient: kabuステーションAPI HTTPクライアント.

設計上の約束（DESIGN.md §3.1）:
- すべてのリクエストにタイムアウトを課す（接続2s / 読み 情報5s・発注10s）
- リトライは取得系のみ（タイムアウト・接続断・429、指数バックオフ）
- **発注系は自動リトライしない**。通信失敗は send_may_have_reached=True で返し、
  突合判断は注文管理層 (P2) に委ねる
- 401 の自動再認証+リプレイも**取得系のみ**（発注のリプレイは二重発注リスク）
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from strata.api.codes import describe, describe_request_check
from strata.api.lockfile import IssuanceLock, LockHeldError
from strata.api.models import (
    Board,
    ErrorResponse,
    OrderItem,
    OrderResult,
    OrderSide,
    PositionsResponse,
    RegisterResult,
    SendOrderRequest,
    SymbolInfo,
    TokenResult,
    WalletCash,
)
from strata.api.ratelimit import ApiCategory, RateLimiterRegistry
from strata.api.results import (
    NETWORK_KIND_CONNECT,
    NETWORK_KIND_PROTOCOL,
    NETWORK_KIND_TIMEOUT,
    NETWORK_KIND_UNEXPECTED,
    ApiFailure,
    ApiSuccess,
    HttpFailure,
    NetworkFailure,
    ParseFailure,
)
from strata.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

ApiResult = ApiSuccess[T] | HttpFailure | ApiFailure | NetworkFailure | ParseFailure
"""API呼び出しの結果ユニオン."""

_RETRY_STATUS = frozenset({429, 502, 503})


class KabuClient:
    """同期HTTPクライアント（1プロセス1インスタンス想定）."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        *,
        limiter: RateLimiterRegistry | None = None,
        issuance_lock_path: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        api = settings.api
        # httpx の相対パス結合はベースの末尾スラッシュ必須
        self._http = httpx.Client(
            base_url=api.base_url.rstrip("/") + "/",
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(
                connect=api.timeout_connect_s,
                read=api.timeout_read_s,
                write=api.timeout_read_s,
                pool=api.timeout_connect_s,
            ),
            transport=transport,
        )
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self._order_read_timeout = api.timeout_order_read_s
        self._retry_max = api.retry_max_attempts
        self._backoff_base = api.retry_backoff_base_s
        self._limiter = limiter or RateLimiterRegistry(
            {
                ApiCategory.ORDER: api.rate_limit_order_per_s,
                ApiCategory.INFO: api.rate_limit_info_per_s,
                ApiCategory.TIMEANDSALES: api.rate_limit_timeandsales_per_s,
            }
        )
        self._issuance_lock_path = issuance_lock_path or Path("var/token.lock")

    # ------------------------------------------------------------------
    # 認証
    # ------------------------------------------------------------------

    @property
    def has_token(self) -> bool:
        return self._token is not None

    def authenticate(self) -> ApiResult[TokenResult]:
        """トークン発行（POST /token）.

        別トークン発行で旧トークンは失効するため、ファイルロックで
        プロセッ間発行直列化する。失敗時も旧トークンは失効している可能性が
        あるため保持しない（None化）。
        """
        result: ApiResult[TokenResult]
        try:
            with IssuanceLock(self._issuance_lock_path):
                result = self._token_request()
        except LockHeldError as exc:
            return NetworkFailure(kind=NETWORK_KIND_CONNECT, message=str(exc))
        if isinstance(result, ApiSuccess):
            with self._token_lock:
                self._token = result.data.Token
        else:
            with self._token_lock:
                self._token = None
        return result

    def _token_request(self) -> ApiResult[TokenResult]:
        payload = {"APIPassword": self._settings.require_api_password()}
        return self._request(
            "POST",
            "token",
            category=ApiCategory.INFO,
            payload=payload,
            model=TokenResult,
            retryable=True,
            order_flow=False,
        )

    def _reauth(self) -> bool:
        """401検知時の再認証。成功時True."""
        result = self.authenticate()
        return isinstance(result, ApiSuccess)

    # ------------------------------------------------------------------
    # 基本リクエスト
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        category: ApiCategory,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        model: type[T] | TypeAdapter[Any] | None = None,
        retryable: bool,
        order_flow: bool,
    ) -> ApiResult[Any]:
        auth_replayed = False
        attempt = 0
        while True:
            attempt += 1
            self._limiter.acquire(category)
            headers = {}
            if self._token is not None:
                headers["X-API-KEY"] = self._token
            timeout = self._order_read_timeout if order_flow else httpx.USE_CLIENT_DEFAULT
            network_failure: NetworkFailure | None = None
            try:
                resp = self._http.request(
                    method,
                    path,
                    params=params,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                network_failure = self._make_network_failure(
                    NETWORK_KIND_TIMEOUT, str(exc), attempt, order_flow
                )
            except httpx.ConnectError as exc:
                network_failure = self._make_network_failure(
                    NETWORK_KIND_CONNECT, str(exc), attempt, order_flow
                )
            except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as exc:
                network_failure = self._make_network_failure(
                    NETWORK_KIND_PROTOCOL, str(exc), attempt, order_flow
                )
            except httpx.HTTPError as exc:  # pragma: no cover - 想定外
                network_failure = self._make_network_failure(
                    NETWORK_KIND_UNEXPECTED, str(exc), attempt, order_flow
                )
            if network_failure is not None:
                # 通信失敗（取得系のみ再試行対象。送信済み可能性のある発注系は即終了）
                if retryable and attempt < self._retry_max:
                    wait = self._backoff_base * (2 ** (attempt - 1))
                    logger.warning(
                        "通信失敗 %s: %.1fs待機して再試行 (%d/%d) %s %s",
                        network_failure.kind,
                        wait,
                        attempt,
                        self._retry_max,
                        method,
                        path,
                    )
                    self._sleep(wait)
                    continue
                logger.error(
                    "通信失敗 (%s, %d回目): %s",
                    network_failure.kind,
                    attempt,
                    network_failure.message,
                )
                return network_failure

            # --- 401: 再認証（リプレイは取得系のみ。発注系は再送しない） ---
            if resp.status_code == 401:
                if not order_flow and not auth_replayed:
                    auth_replayed = True
                    if self._reauth():
                        logger.info("401検知: 再認証して1回リプレイします (%s %s)", method, path)
                        continue
                elif order_flow:
                    # トークン自体は更新するが、この発注は絶対にリプレイしない
                    self._reauth()
                return self._http_failure(resp)

            # --- リトライ対象ステータス（取得系のみ） ---
            if resp.status_code in _RETRY_STATUS and retryable and attempt < self._retry_max:
                wait = self._backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "HTTP %s: %.1fs待機して再試行 (%d/%d) %s %s",
                    resp.status_code,
                    wait,
                    attempt,
                    self._retry_max,
                    method,
                    path,
                )
                self._sleep(wait)
                continue

            if resp.status_code >= 400:
                return self._http_failure(resp)

            return self._parse_success(resp, model, order_flow=order_flow)

    def _make_network_failure(
        self, kind: str, message: str, attempts: int, order_flow: bool
    ) -> NetworkFailure:
        return NetworkFailure(
            kind=kind,
            message=message,
            attempts=attempts,
            send_may_have_reached=order_flow,
        )

    def _http_failure(self, resp: httpx.Response) -> HttpFailure:
        code: int | None = None
        message = resp.reason_phrase or ""
        try:
            err = ErrorResponse.model_validate_json(resp.content)
            code = err.Code
            if err.Message:
                message = err.Message
        except (ValidationError, ValueError):
            pass
        if code is not None:
            message = f"{message} ({describe_request_check(code).message})"
        return HttpFailure(status=resp.status_code, code=code, message=message)

    def _parse_success(
        self,
        resp: httpx.Response,
        model: type[Any] | TypeAdapter[Any] | None,
        *,
        order_flow: bool = False,
    ) -> ApiResult[Any]:
        if model is None:
            return ApiSuccess(data=None)
        try:
            if isinstance(model, TypeAdapter):
                data = model.validate_json(resp.content)
            else:
                data = model.model_validate_json(resp.content)
        except ValidationError as exc:
            logger.error("レスポンス解析失敗: %s raw=%r", exc, resp.text[:200])
            return ParseFailure(
                message=str(exc), raw=resp.text[:500], send_may_have_reached=order_flow
            )

        # 业务コード判定（HTTP 200 でも Result / ResultCode がエラーの場合がある）
        if isinstance(data, TokenResult) and data.ResultCode != 0:
            info = describe_request_check(data.ResultCode)
            logger.error("トークン取得失敗: %s (%s)", data.ResultCode, info.message)
            return ApiFailure(code=data.ResultCode, message=info.message, kind="token")
        if isinstance(data, OrderResult) and data.Result != 0:
            info = describe(data.Result)
            logger.error("発注リジェクト: Result=%s (%s)", data.Result, info.message)
            return ApiFailure(code=data.Result, message=info.message, kind="order")
        return ApiSuccess(data=data)

    # ------------------------------------------------------------------
    # 情報系エンドポイント（取得系: リトライ・401リプレイ有効）
    # ------------------------------------------------------------------

    def wallet_cash(self) -> ApiResult[WalletCash]:
        return self._request(
            "GET",
            "wallet/cash",
            category=ApiCategory.INFO,
            model=WalletCash,
            retryable=True,
            order_flow=False,
        )

    def board(self, symbol: str, market: int = 1) -> ApiResult[Board]:
        return self._request(
            "GET",
            f"board/{symbol}@{market}",
            category=ApiCategory.INFO,
            model=Board,
            retryable=True,
            order_flow=False,
        )

    def symbol_info(self, symbol: str, market: int = 1) -> ApiResult[SymbolInfo]:
        return self._request(
            "GET",
            f"symbol/{symbol}@{market}",
            category=ApiCategory.INFO,
            model=SymbolInfo,
            retryable=True,
            order_flow=False,
        )

    def orders(
        self,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        state: int | None = None,
        side: OrderSide | None = None,
        updtime: str | None = None,
    ) -> ApiResult[list[OrderItem]]:
        """注文約定照会（GET /orders）."""
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if order_id is not None:
            params["id"] = order_id
        if state is not None:
            params["state"] = state
        if side is not None:
            params["side"] = 1 if side is OrderSide.SELL else 2
        if updtime is not None:
            params["updtime"] = updtime
        return self._request(
            "GET",
            "orders",
            category=ApiCategory.INFO,
            params=params or None,
            model=TypeAdapter(list[OrderItem]),
            retryable=True,
            order_flow=False,
        )

    def positions(self) -> ApiResult[PositionsResponse]:
        return self._request(
            "GET",
            "positions",
            category=ApiCategory.INFO,
            params={"addinfo": "true"},
            model=PositionsResponse,
            retryable=True,
            order_flow=False,
        )

    def register(self, symbols: list[tuple[str, int]]) -> ApiResult[RegisterResult]:
        payload = {"Symbols": [{"Symbol": s, "Exchange": m} for s, m in symbols]}
        return self._request(
            "PUT",
            "register",
            category=ApiCategory.INFO,
            payload=payload,
            model=RegisterResult,
            retryable=True,
            order_flow=False,
        )

    def unregister_all(self) -> ApiResult[RegisterResult]:
        return self._request(
            "PUT",
            "unregister/all",
            category=ApiCategory.INFO,
            model=RegisterResult,
            retryable=True,
            order_flow=False,
        )

    # ------------------------------------------------------------------
    # 発注系（自動リトライ・401リプレイなし）
    # ------------------------------------------------------------------

    def sendorder(self, request: SendOrderRequest) -> ApiResult[OrderResult]:
        """現物注文の送信.

        NetworkFailure を返した場合、注文はブローカーに到達している可能性がある。
        呼び出し側（注文管理層）は再送せず /orders 突合で確定させること。
        """
        return self._request(
            "POST",
            "sendorder",
            category=ApiCategory.ORDER,
            payload=request.to_payload(),
            model=OrderResult,
            retryable=False,
            order_flow=True,
        )

    def cancelorder(self, order_id: str) -> ApiResult[OrderResult]:
        return self._request(
            "PUT",
            "cancelorder",
            category=ApiCategory.ORDER,
            payload={"OrderId": order_id},
            model=OrderResult,
            retryable=False,
            order_flow=True,
        )

    # ------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KabuClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
