"""② データ層の共通基盤: バーモデルとプロバイダインターフェース."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

BarSource = Literal["jquants", "yfinance", "manual"]


class DataProviderError(RuntimeError):
    """データプロバイダ側の障害（認証・応答形式・通信）."""


class DailyBar(BaseModel):
    """1銘柄1営業日バー。DBの `bars_daily` と1対1対応する."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    #: 営業日 YYYY-MM-DD (JST)
    date: str
    open: float = Field(ge=0)
    high: float = Field(ge=0)
    low: float = Field(ge=0)
    close: float = Field(ge=0)
    #: 出来高。出典に存在しない場合（J-Quants日次など）は0
    volume: int = Field(default=0, ge=0)
    source: BarSource


class MarketDataProvider(ABC):
    """日足データ出典のインターフェース。

    実装は必ず「期間内の営業日バーのみ・日付昇順」で返すこと。
    品質保証はプロバイダの責務ではなくバリデータ（validate.py）の責務。
    """

    #: DBの bars_daily.source に一致させる識別子
    name: ClassVar[str]

    @abstractmethod
    def fetch_daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        """[start, end] 閉区間の日线を取得する。失敗は DataProviderError."""

    def close(self) -> None:  # noqa: B027 - 意図的なデフォルト実装
        """リソース解放（HTTPセッション等）。既定では何もしない."""

    def __enter__(self) -> MarketDataProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def to_dot_symbol(symbol: str) -> str:
    """証券コードを「8697.T」形式へ正規化（東証=東京のみ想定）.

    Yahoo指数コード（^TOPX 等、先頭が ^）は変換せずそのまま返す。
    """
    code = symbol.split("@")[0].strip()
    if code.startswith("^"):
        return code
    return code if code.endswith(".T") else f"{code}.T"


def to_code(symbol: str) -> str:
    """証券コードを4-5桁の素のコードへ正規化."""
    return symbol.split("@")[0].split(".")[0].strip()
