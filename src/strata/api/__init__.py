"""① APIクライアント層.

kabuステーションAPIへのHTTPアクセスの唯一の出入口。
タイムアウト・トークン管理・レート制限・リトライ方針をここで一元化する。
"""

from strata.api.client import ApiResult, KabuClient
from strata.api.models import OrderSide, PriceType, SendOrderRequest
from strata.api.results import (
    ApiFailure,
    ApiSuccess,
    HttpFailure,
    NetworkFailure,
    ParseFailure,
)

__all__ = [
    "ApiFailure",
    "ApiResult",
    "ApiSuccess",
    "HttpFailure",
    "KabuClient",
    "NetworkFailure",
    "OrderSide",
    "ParseFailure",
    "PriceType",
    "SendOrderRequest",
]
