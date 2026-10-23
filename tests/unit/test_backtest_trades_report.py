"""約定→トレード再構築（trades.py）と有効性レポート（report.py）のテスト.

TESTING §3.7: FIFO対応・部分約定・閉じていない玉・集計（勝率/PF/期待値）と
CSV・Markdown 出力を確認する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.backtest.report import (
    group_stats,
    key_entry_source,
    key_symbol,
    render_markdown,
    summarize,
    write_trades_csv,
)
from strata.backtest.trades import Fill, build_trades, normalize_date


def fill(
    side: str,
    qty: int,
    price: float,
    day: str,
    *,
    symbol: str = "8306",
    source: str = "ma_cross",
    reason: str = "",
) -> Fill:
    return Fill(
        traded_at=f"{day}153000",
        date=normalize_date(f"{day}153000"),
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        source=source,
        reason=reason or f"{side}@{day}",
    )


class TestNormalizeDate:
    def test_形式いろいろ(self) -> None:
        assert normalize_date("2026-10-23") == "2026-10-23"
        assert normalize_date("20261023A01N") == "2026-10-23"
        assert normalize_date("2026-10-23T06:00:00+00:00") == "2026-10-23"

    def test_不正(self) -> None:
        with pytest.raises(ValueError):
            normalize_date("not-a-date")


class TestBuildTrades:
    def test_単純往復と損益(self) -> None:
        closed, open_trades = build_trades(
            [fill("BUY", 100, 1000.0, "2026-01-05"), fill("SELL", 100, 1100.0, "2026-01-20")]
        )
        assert open_trades == []
        assert len(closed) == 1
        t = closed[0]
        assert t.pnl == 10_000
        assert t.return_pct == pytest.approx(0.1)
        assert t.hold_days == 15
        assert t.entry_source == "ma_cross"
        assert t.exit_source == "ma_cross"

    def test_分割買いはFIFO(self) -> None:
        closed, open_trades = build_trades(
            [
                fill("BUY", 100, 1000.0, "2026-01-05"),
                fill("BUY", 100, 1200.0, "2026-01-06"),
                fill("SELL", 150, 1300.0, "2026-01-10"),
            ]
        )
        assert len(closed) == 2  # 第1ロット100 + 第2ロット50
        assert closed[0].entry_price == 1000.0
        assert closed[0].qty == 100
        assert closed[1].entry_price == 1200.0
        assert closed[1].qty == 50
        assert len(open_trades) == 1
        assert open_trades[0].qty == 50  # 第2ロットの残余

    def test_売り先行は不整合として例外(self) -> None:
        with pytest.raises(ValueError):
            build_trades([fill("SELL", 100, 1000.0, "2026-01-05")])

    def test_銘柄別に分かれる(self) -> None:
        closed, open_trades = build_trades(
            [
                fill("BUY", 100, 100.0, "2026-01-05", symbol="7203"),
                fill("BUY", 100, 200.0, "2026-01-05", symbol="9984"),
                fill("SELL", 100, 150.0, "2026-01-10", symbol="7203"),
            ]
        )
        assert [t.symbol for t in closed] == ["7203"]
        assert {lot.symbol for lot in open_trades} == {"9984"}


class TestSummarize:
    def _trades(self):  # type: ignore[no-untyped-def]
        closed, _ = build_trades(
            [
                fill("BUY", 100, 1000.0, "2026-01-05"),
                fill("SELL", 100, 1100.0, "2026-01-06"),  # +10000
                fill("BUY", 100, 1000.0, "2026-01-07"),
                fill("SELL", 100, 950.0, "2026-01-08"),  # -5000
            ]
        )
        return closed

    def test_勝率と期待値とPF(self) -> None:
        stat = summarize(self._trades())
        assert stat.trades == 2
        assert stat.win_rate == pytest.approx(0.5)
        assert stat.total_pnl == 5000
        assert stat.expected_value == 2500
        assert stat.profit_factor == pytest.approx(2.0)
        assert stat.max_loss == -5000
        assert stat.max_gain == 10000

    def test_損失ゼロはPF無限大(self) -> None:
        closed, _ = build_trades(
            [fill("BUY", 100, 100.0, "2026-01-05"), fill("SELL", 100, 101.0, "2026-01-06")]
        )
        stat = summarize(closed)
        assert stat.profit_factor == float("inf")

    def test_グループ化(self) -> None:
        groups = group_stats(self._trades(), key_symbol)
        assert set(groups) == {"8306"}
        by_source = group_stats(self._trades(), key_entry_source)
        assert by_source["ma_cross"].trades == 2


class TestOutputs:
    def test_CSV書き出し(self, tmp_path: Path) -> None:
        closed, _ = build_trades(
            [fill("BUY", 100, 1000.0, "2026-01-05"), fill("SELL", 100, 900.0, "2026-01-06")]
        )
        path = write_trades_csv(closed, tmp_path / "nested" / "trades.csv")
        text = path.read_text(encoding="utf-8")
        assert text.startswith("symbol,entry_date,entry_price")
        assert "8306" in text and "-10000.0" in text

    def test_Markdown描画(self) -> None:
        closed, open_trades = build_trades(
            [
                fill("BUY", 100, 1000.0, "2026-01-05", source="rsi_reversion"),
                fill("SELL", 100, 1200.0, "2026-01-06", source="ma_cross"),
                fill("BUY", 100, 500.0, "2026-01-05", symbol="9984", source="rsi_reversion"),
            ]
        )
        md = render_markdown(
            strategy_name="demo",
            metrics={"total_return": 0.02, "max_drawdown": 0.01},
            trades=closed,
            open_trades=open_trades,
            by_entry_source=group_stats(closed, key_entry_source),
        )
        assert "# バックテスト有効性レポート: demo" in md
        assert "エントリー根拠別" in md
        assert "rsi_reversion" in md
        assert "未決済の買い玉" in md
        assert "9984" in md
