"""kabuステーションAPI のリクエスト/レスポンスモデル（pydantic）.

- フィールド名は公式フィールド名（PascalCase）をそのまま使用する
- 未知フィールドは許容（仕様改定耐性）。必要なものだけ型で守る
- リクエスト側は送信前にランタイム検証する（二重・三重の防御）
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SYMBOL_RE = re.compile(r"^[0-9A-Z]{2,12}$")

#: 株式の発注可能市場（1=東証は通常発注不可のため既定から除外）
STOCK_EXCHANGES = (3, 5, 6, 9, 27)
EXCHANGE_TSE = 1
EXCHANGE_SOR = 9


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def api_value(self) -> str:
        """公式の Side パラメータ（"1"=売, "2"=買）へ変換."""
        return "2" if self is OrderSide.BUY else "1"


class PriceType(StrEnum):
    """執行条件（FrontOrderType）のドメイン側表現（株式）."""

    MARKET = "MARKET"  # 10 成行
    LIMIT = "LIMIT"  # 20 指値
    AT_OPEN_MARKET = "AT_OPEN_MARKET"  # 13 寄成前
    AT_OPEN_LIMIT = "AT_OPEN_LIMIT"  # 21 寄指前
    AT_CLOSE_MARKET = "AT_CLOSE_MARKET"  # 16 引成後
    AT_CLOSE_LIMIT = "AT_CLOSE_LIMIT"  # 24 引指後


PRICE_TYPE_TO_FRONT_ORDER_TYPE: dict[PriceType, int] = {
    PriceType.MARKET: 10,
    PriceType.LIMIT: 20,
    PriceType.AT_OPEN_MARKET: 13,
    PriceType.AT_OPEN_LIMIT: 21,
    PriceType.AT_CLOSE_MARKET: 16,
    PriceType.AT_CLOSE_LIMIT: 24,
}


# ---------------------------------------------------------------------------
# リクエスト
# ---------------------------------------------------------------------------


class SendOrderRequest(BaseModel):
    """現物注文発注リクエスト（P0/P1は現物のみ）.

    検証内容（送信前のクライアント側防御）:
    - 数量 > 0
    - 成行系は price==0、指値系は price>0
    - 東証(1)指定は SOR(9) へ読み替え（公式制限: 通常時は東証直接発注不可）
    - 現物売の FundType="  "（半角スペース2つ）補完
    """

    model_config = ConfigDict(validate_assignment=True)

    symbol: str
    #: 市場。既定はSOR。東証(1)はSORへ読み替える
    exchange: int = EXCHANGE_SOR
    side: OrderSide
    qty: int = Field(gt=0)
    price_type: PriceType = PriceType.MARKET
    price: float = Field(default=0.0, ge=0)
    #: 口座区分: 2=一般, 4=特定, 12=法人
    account_type: int = 4
    #: 受渡区分: 0=指定なし, 2=お預り金。Noneで売買区分から自動
    deliv_type: int | None = None
    #: 資産区分（Noneで自動: 買=省略, 売="  "）
    fund_type: str | None = None
    #: 有効期限 (0=本日)
    expire_day: int = 0

    @model_validator(mode="after")
    def _validate_and_fix(self) -> SendOrderRequest:
        if not _SYMBOL_RE.match(self.symbol):
            raise ValueError(f"銘柄コードが不正です: {self.symbol!r}")
        if self.exchange == EXCHANGE_TSE:
            # 公式: 通常時は東証指定での新規発注不可（100378）。SORへ読替
            object.__setattr__(self, "exchange", EXCHANGE_SOR)
        if self.exchange not in STOCK_EXCHANGES:
            raise ValueError(f"株式発注不可の市場です: {self.exchange}")
        if self.price_type in (
            PriceType.MARKET,
            PriceType.AT_OPEN_MARKET,
            PriceType.AT_CLOSE_MARKET,
        ):
            if self.price != 0:
                raise ValueError(f"{self.price_type} では価格を0にする必要があります")
        elif self.price <= 0:
            raise ValueError(f"{self.price_type} では価格>0が必要です")
        if self.account_type not in (2, 4, 12):
            raise ValueError(f"口座区分が不正です: {self.account_type}")
        return self

    @property
    def resolved_deliv_type(self) -> int:
        if self.deliv_type is not None:
            return self.deliv_type
        # 現物買は受渡区分必須（お預り金）、現物売は指定なし
        return 2 if self.side is OrderSide.BUY else 0

    @property
    def resolved_fund_type(self) -> str | None:
        if self.fund_type is not None:
            return self.fund_type
        return "  " if self.side is OrderSide.SELL else None

    def to_payload(self) -> dict[str, Any]:
        """公式 sendorder リクエストボディへ変換."""
        payload: dict[str, Any] = {
            "Symbol": self.symbol,
            "Exchange": self.exchange,
            "SecurityType": 1,
            "Side": self.side.api_value,
            "CashMargin": 1,  # 現物固定（拡張時はここを開く）
            "DelivType": self.resolved_deliv_type,
            "AccountType": self.account_type,
            "Qty": self.qty,
            "Price": self.price,
            "ExpireDay": self.expire_day,
            "FrontOrderType": PRICE_TYPE_TO_FRONT_ORDER_TYPE[self.price_type],
        }
        fund = self.resolved_fund_type
        if fund is not None:
            payload["FundType"] = fund
        return payload


# ---------------------------------------------------------------------------
# レスポンス（業務成功時のみパースする。失敗系は results.* 側で扱う）
# ---------------------------------------------------------------------------


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TokenResult(ResponseModel):
    ResultCode: int
    Token: str = ""


class OrderResult(ResponseModel):
    Result: int
    OrderId: str | None = None


class ErrorResponse(ResponseModel):
    ResultCode: int | None = None
    Code: int | None = None
    Message: str | None = None


class WalletCash(ResponseModel):
    Payable: float | None = None
    MarginUsable: float | None = None
    MarginBalance: float | None = None
    Deposit: float | None = None


class BoardDetail(ResponseModel):
    Price: float | None = None
    Qty: int | None = None


class Board(ResponseModel):
    Symbol: str | None = None
    Exchange: int | None = None
    CurrentPrice: float | None = None
    OpeningPrice: float | None = None
    TradingVolume: int | None = None
    BidPrice: BoardDetail | None = None
    AskPrice: BoardDetail | None = None
    SecurityType: int | None = None


class SymbolInfo(ResponseModel):
    Symbol: str | None = None
    SymbolName: str | None = None
    TradingUnit: int | None = None
    PriceRangeGroup: str | None = None


class OrderDetailItem(ResponseModel):
    SeqNum: int | None = None
    ID: str | None = None
    RecType: int | None = None  # 1受付/3期限切れ/4発注/5訂正/6取消/7失効/8約定
    ExchangeID: str | None = None
    State: int | None = None
    Price: float | None = None
    Qty: int | None = None
    ExecutionID: str | None = None
    TransactTime: str | None = None
    DelivDay: int | None = None
    Commission: float | None = None
    CommissionTax: float | None = None


class OrderItem(ResponseModel):
    ID: str | None = None
    State: int | None = None  # 1待機/2処理中/3処理済/4訂正取消送信中/5終了
    RecvTime: str | None = None  # yyyyMMddHHmmss（突合の時刻順判定用）
    Symbol: str | None = None
    Exchange: int | None = None
    Side: str | None = None  # "1"売/"2"買
    Price: float | None = None
    OrderQty: int | None = None
    CumQty: int | None = None
    CashMargin: int | None = None
    Details: list[OrderDetailItem] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.State == 5

    @property
    def cum_qty(self) -> int:
        return self.CumQty or 0

    @property
    def order_qty(self) -> int:
        return self.OrderQty or 0

    @property
    def fully_filled(self) -> bool:
        return self.is_terminal and self.order_qty > 0 and self.cum_qty >= self.order_qty

    def fills(self) -> list[OrderDetailItem]:
        """約定明細（RecType=8）のみ返す."""
        return [d for d in self.Details if d.RecType == 8]


class PositionItem(ResponseModel):
    ExecutionID: str | None = None
    Symbol: str | None = None
    Exchange: int | None = None
    Side: str | None = None
    Price: float | None = None
    HoldQty: int | None = None
    LeavesQty: int | None = None
    CurrentPrice: float | None = None
    ProfitLoss: float | None = None


class PositionsResponse(ResponseModel):
    Holdings: list[PositionItem] = Field(default_factory=list)


class RegisterResult(ResponseModel):
    class RegistSymbol(ResponseModel):
        Symbol: str | None = None
        Exchange: int | None = None

    RegistList: list[RegistSymbol] = Field(default_factory=list)
