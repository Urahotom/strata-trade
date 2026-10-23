"""プロセス内ペーパーブローカー（バックテスト用の仮定Broker）.

`OrderManager` が実 `KabuClient` と呼ぶのと同じプロトコル
（sendorder / orders / cancelorder / positions / authenticate）を実装し、
バックテストが **運用と同一の意図台帳コードパス** を通るようにする
（TESTING §3.4）。約定モデル:

- 成行は「気配=当日終値」で全量約定（スリッページ・手数料なしは明示的妥協）
- FillPolicy.NEXT_OPEN: 当日シグナルの発注は翌営業日（次回 set_market_day）に約定
- 買付可能額不足は実APIと同じ Result=21 でリジェクトする
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from strata.api.models import (
    OrderDetailItem,
    OrderItem,
    OrderResult,
    OrderSide,
    PositionItem,
    PositionsResponse,
    PriceType,
    SendOrderRequest,
    TokenResult,
)
from strata.api.results import ApiFailure, ApiSuccess

_SIDE_CODE = {OrderSide.BUY: "2", OrderSide.SELL: "1"}


class FillPolicy(StrEnum):
    #: 発注翌営業日（次回 set_market_day）に終値約定（終値ベース戦略の標準）
    NEXT_OPEN = "next_open"
    #: 発注日（当日終値）に即時約定（楽観的。検証用）
    CLOSE = "close"


@dataclass
class _PaperOrder:
    order_id: str
    request: SendOrderRequest
    submitted_day: str
    side_code: str
    state: int = 1  # 1待機 → 5終了
    filled_qty: int = 0
    details: list[OrderDetailItem] = field(default_factory=list)
    seq: int = 0

    @property
    def day_compact(self) -> str:
        return self.submitted_day.replace("-", "")


class PaperBroker:
    """OrderManagerから見てKabuClientと同一面を持つ仮定ブローカー."""

    def __init__(
        self,
        *,
        initial_cash: float = 1_000_000.0,
        fill: FillPolicy = FillPolicy.NEXT_OPEN,
    ) -> None:
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.fill_policy = fill
        #: 建玉 {symbol: 数量}（現物買いのみ）
        self.holdings: dict[str, int] = {}
        #: 取得単価（加重平均） {symbol: 円}
        self.avg_price: dict[str, float] = {}
        #: 確定損益の累計（売却 realization）
        self.realized_pnl = 0.0
        #: 往復完結トレード数（売却で確定した分）
        self.round_trips = 0
        self.winning_exits = 0
        self._orders: dict[str, _PaperOrder] = {}
        self._order_seq = 0
        self._exec_seq = 0
        self._day: str | None = None
        self._day_prices: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 運用側から呼ばれるプロトコル
    # ------------------------------------------------------------------ #

    def authenticate(self) -> ApiSuccess[TokenResult]:
        return ApiSuccess(TokenResult(ResultCode=0, Token="paper"))

    def sendorder(self, request: SendOrderRequest) -> ApiSuccess[OrderResult] | ApiFailure:
        assert self._day is not None, "set_market_day() を先に呼んでください"
        if request.price_type is not PriceType.MARKET:
            return ApiFailure(code=100216, message="仮定Brokerは成行のみ", kind="order")
        price = self._price(request.symbol)
        if request.side is OrderSide.BUY:
            notional = price * request.qty
            if notional > self.cash + 1e-9:
                return ApiFailure(code=21, message="買付可能額を超えています", kind="order")
        else:
            if self.holdings.get(request.symbol, 0) < request.qty:
                return ApiFailure(code=21, message="売付可能数量不足", kind="order")
        self._order_seq += 1
        order_id = f"PB{self._order_seq:07d}"
        order = _PaperOrder(
            order_id=order_id,
            request=request,
            submitted_day=self._day,
            side_code=_SIDE_CODE[request.side],
        )
        self._orders[order_id] = order
        if self.fill_policy is FillPolicy.CLOSE:
            self._fill(order, price)
        return ApiSuccess(OrderResult(Result=0, OrderId=order_id))

    def orders(
        self,
        *,
        order_id: str | None = None,
        symbol: str | None = None,
        state: int | None = None,
        side: str | None = None,
        **_kw: object,
    ) -> ApiSuccess[list[OrderItem]]:
        items = [self._to_item(o) for o in self._orders.values()]
        if order_id:
            items = [i for i in items if order_id == i.ID]
        if symbol:
            items = [i for i in items if i.Symbol == symbol.split("@")[0]]
        if state is not None:
            items = [i for i in items if i.State == state]
        if side is not None:
            items = [i for i in items if i.Side == side]
        return ApiSuccess(items)

    def cancelorder(self, order_id: str) -> ApiSuccess[OrderResult] | ApiFailure:
        order = self._orders.get(order_id)
        if order is None or order.state == 5:
            return ApiFailure(code=41, message="指定期明が存在しません", kind="order")
        order.state = 5
        order.seq += 1
        order.details.append(OrderDetailItem(SeqNum=order.seq, RecType=6, State=3))
        return ApiSuccess(OrderResult(Result=0, OrderId=order_id))

    def positions(self) -> ApiSuccess[PositionsResponse]:
        holdings = [
            PositionItem(
                Symbol=symbol,
                Exchange=9,
                Side="2",
                HoldQty=qty,
                Price=self._price(symbol),
                CurrentPrice=self._price(symbol),
                ProfitLoss=(self._price(symbol) - self.avg_price.get(symbol, 0.0)) * qty,
            )
            for symbol, qty in sorted(self.holdings.items())
            if qty > 0
        ]
        return ApiSuccess(PositionsResponse(Holdings=holdings))

    # ------------------------------------------------------------------ #
    # テスターナー側ユーティリティ
    # ------------------------------------------------------------------ #

    def set_market_day(self, day: str, closes: Mapping[str, float]) -> int:
        """新しい営業日を開始し、約定待ちを気配（終値）で約定させる.

        Returns:
            この日で約定（または失効処理）した注文数
        """
        assert day > (self._day or ""), f"日付は前进のみです: {self._day} -> {day}"
        self._day = day
        self._day_prices = dict(closes)
        settled = 0
        for order in self._orders.values():
            if order.state != 1:
                continue
            if self.fill_policy is FillPolicy.NEXT_OPEN and order.submitted_day >= day:
                continue  # 当日発注は約定待ちのまま
            self._fill(order, self._price(order.request.symbol))
            settled += 1
        return settled

    def held_qty(self, symbol: str) -> int:
        return self.holdings.get(symbol, 0)

    def price_of(self, symbol: str) -> float:
        """直近 set_market_day で設定された気配（終値）."""
        return self._price(symbol)

    def equity(self) -> float:
        return self.cash + sum(qty * self._price(symbol) for symbol, qty in self.holdings.items())

    # ------------------------------------------------------------------ #

    def _price(self, symbol: str) -> float:
        return self._day_prices.get(symbol, 0.0)

    def _fill(self, order: _PaperOrder, price: float) -> None:
        request = order.request
        qty = request.qty
        self._exec_seq += 1
        order.filled_qty = qty
        order.state = 5
        order.seq += 1
        order.details.append(
            OrderDetailItem(
                SeqNum=order.seq,
                RecType=8,
                State=3,
                Price=price,
                Qty=qty,
                ExecutionID=f"PBX-{order.order_id}",
                TransactTime=f"{order.day_compact}153000",
            )
        )
        if request.side is OrderSide.BUY:
            cost = price * qty
            self.cash -= cost
            total_qty = self.holdings.get(request.symbol, 0) + qty
            base = self.avg_price.get(request.symbol, 0.0) * self.holdings.get(request.symbol, 0)
            self.avg_price[request.symbol] = (base + cost) / total_qty
            self.holdings[request.symbol] = total_qty
        else:
            self.cash += price * qty
            avg = self.avg_price.get(request.symbol, 0.0)
            pnl = (price - avg) * qty
            self.realized_pnl += pnl
            self.round_trips += 1
            if pnl > 0:
                self.winning_exits += 1
            self.holdings[request.symbol] = max(0, self.holdings.get(request.symbol, 0) - qty)

    def _to_item(self, order: _PaperOrder) -> OrderItem:
        return OrderItem(
            ID=order.order_id,
            State=order.state,
            RecvTime=f"{order.day_compact}090000",
            Symbol=order.request.symbol,
            Exchange=9,
            Side=order.side_code,
            Price=0.0,  # 成行
            OrderQty=order.request.qty,
            CumQty=order.filled_qty,
            CashMargin=1,
            Details=list(order.details),
        )


__all__ = ["FillPolicy", "PaperBroker"]
