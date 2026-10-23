"""API呼び出し成否の判別結果型.

例外ではなく結果型で返し、呼び出し側（注文管理層）が
「送信済みか不定」を explicit に扱えるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")

#: 通信失敗の分類
NETWORK_KIND_TIMEOUT = "timeout"
NETWORK_KIND_CONNECT = "connect"
NETWORK_KIND_PROTOCOL = "protocol"  # 応答喪失・接続リセットを含む
NETWORK_KIND_UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class ApiSuccess(Generic[T]):
    """HTTP 200 かつ业务上も成功した応答."""

    data: T


@dataclass(frozen=True)
class HttpFailure:
    """HTTP レベルのエラー（401/403/404/429/500 等）."""

    status: int
    code: int | None
    message: str

    @property
    def is_auth_error(self) -> bool:
        return self.status == 401

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429


@dataclass(frozen=True)
class ApiFailure:
    """HTTP 200 だが业务コード（ResultCode / Result）がエラーの応答."""

    code: int
    message: str
    #: 種別: "token" / "order" / "request"
    kind: str = "request"

    @property
    def is_rejection(self) -> bool:
        return self.kind == "order"


@dataclass(frozen=True)
class NetworkFailure:
    """通信層の失敗。送信は完了しているが応答が不明な可能性がある."""

    kind: str  # NETWORK_KIND_* のいずれか
    message: str
    attempts: int = 1
    #: 発注系で発生した場合 True。安易な再送禁止の合図
    send_may_have_reached: bool = False


@dataclass(frozen=True)
class ParseFailure:
    """レスポンスのJSONデコード/モデル検証に失敗.

    発注系で発生した場合（応答が途中で切れた等）は送信済み可能性が
    あるため、NetworkFailure と同じく再送禁止の対象とする。
    """

    message: str
    raw: str = field(default="")
    send_may_have_reached: bool = False


ApiFailureLike = HttpFailure | ApiFailure | NetworkFailure | ParseFailure
"""成否判別用のユニオン。ApiSuccess 以外が失敗系."""
