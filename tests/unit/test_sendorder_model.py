"""発注リクエストモデルの検証ルールのテスト."""

from __future__ import annotations

import pytest

from strata.api.models import (
    EXCHANGE_SOR,
    EXCHANGE_TSE,
    OrderSide,
    PriceType,
    SendOrderRequest,
)


def test_既定はSOR() -> None:
    req = SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100)
    assert req.exchange == EXCHANGE_SOR


def test_東証指定はSORへ読み替え() -> None:
    req = SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100, exchange=EXCHANGE_TSE)
    assert req.exchange == EXCHANGE_SOR


def test_発注不可の市場は拒否() -> None:
    with pytest.raises(ValueError, match="市場"):
        SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100, exchange=2)  # 日通し


def test_数量は正() -> None:
    with pytest.raises(ValueError):
        SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=0)
    with pytest.raises(ValueError):
        SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=-1)


def test_成行で価格指定は拒否() -> None:
    with pytest.raises(ValueError, match="価格を0"):
        SendOrderRequest(
            symbol="8306",
            side=OrderSide.BUY,
            qty=100,
            price_type=PriceType.AT_CLOSE_MARKET,
            price=100,
        )


def test_指値で価格0は拒否() -> None:
    with pytest.raises(ValueError, match="価格>0"):
        SendOrderRequest(
            symbol="8306", side=OrderSide.BUY, qty=100, price_type=PriceType.LIMIT, price=0
        )


def test_銘柄コード不正は拒否() -> None:
    with pytest.raises(ValueError):
        SendOrderRequest(symbol="かぶ", side=OrderSide.BUY, qty=100)


def test_口座区分の範囲外は拒否() -> None:
    with pytest.raises(ValueError):
        SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100, account_type=9)


def test_現物買は受渡区分自動でお預り金() -> None:
    req = SendOrderRequest(symbol="8306", side=OrderSide.BUY, qty=100)
    assert req.resolved_deliv_type == 2
    assert req.resolved_fund_type is None


def test_現物売は受渡区分0とFundTypeスペース2つ() -> None:
    req = SendOrderRequest(symbol="8306", side=OrderSide.SELL, qty=100)
    assert req.resolved_deliv_type == 0
    assert req.resolved_fund_type == "  "


def test_ペイロードは公式キーとFrontOrderType() -> None:
    req = SendOrderRequest(
        symbol="8306",
        side=OrderSide.SELL,
        qty=100,
        price_type=PriceType.AT_CLOSE_MARKET,
    )
    payload = req.to_payload()
    assert payload == {
        "Symbol": "8306",
        "Exchange": EXCHANGE_SOR,
        "SecurityType": 1,
        "Side": "1",
        "CashMargin": 1,
        "DelivType": 0,
        "AccountType": 4,
        "Qty": 100,
        "Price": 0,
        "ExpireDay": 0,
        "FrontOrderType": 16,  # 引成後
        "FundType": "  ",
    }


def test_売買区分の公式変換() -> None:
    assert OrderSide.BUY.api_value == "2"
    assert OrderSide.SELL.api_value == "1"
