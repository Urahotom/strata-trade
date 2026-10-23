"""BacktestRunner のトレード帰属（戦略名・根拠）と有効性出力の end-to-end テスト.

レンジ相場（RSIに有利）で RSI 戦略を回し、fills→trades→CSV/レポートまでが
途切れずに繋がることを確認する（TESTING §3.7）。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

from strata.backtest import (
    BacktestRunner,
    BacktestSettings,
    FillPolicy,
    group_stats,
    key_entry_source,
    write_trades_csv,
)
from strata.config import RiskSettings, WatchlistEntry
from strata.data.base import DailyBar
from strata.strategy import RSIMeanReversion

RISK = RiskSettings(max_notional_per_order=5_000_000, max_qty_per_order=2_000)


def range_bars(symbol: str, n: int = 180) -> list[DailyBar]:
    out: list[DailyBar] = []
    d = date(2025, 1, 6)
    for i in range(n):
        c = 5000 + 420 * math.sin(i / 7.0)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(
            DailyBar(
                symbol=symbol,
                date=d.isoformat(),
                open=c,
                high=c * 1.005,
                low=c * 0.995,
                close=c,
                volume=1000,
                source="manual",
            )
        )
        d += timedelta(days=1)
    return out


def test_RSI戦略でトレードが構築され戦略名が付く(tmp_path: Path) -> None:
    bars = range_bars("1001")
    runner = BacktestRunner(
        strategy=RSIMeanReversion(period=14, lower=30, upper=70),
        universe=[WatchlistEntry(symbol="1001", trading_unit=100, stop_width=20)],
        bars={"1001": bars},
        risk_settings=RISK,
        settings=BacktestSettings(initial_cash=3_000_000, fill=FillPolicy.CLOSE),
    )
    result = runner.run()

    assert result.fills, "レンジ相場では RSI トレードが成立するはず"
    assert {f.source for f in result.fills} == {"rsi_reversion"}
    assert result.trades, "買い→売りの往復が対応付くはず"
    for trade in result.trades:
        assert trade.entry_source == "rsi_reversion"
        assert trade.exit_source == "rsi_reversion"
        assert trade.entry_reason and trade.exit_reason
    # 約定数とトレード対応の整合: 売り約定 = 往復対応 + 未決済分の前書きなし
    sell_qty = sum(f.qty for f in result.fills if f.side == "SELL")
    closed_qty = sum(t.qty for t in result.trades)
    assert closed_qty <= sell_qty
    assert closed_qty > 0

    groups = group_stats(result.trades, key_entry_source)
    assert groups["rsi_reversion"].trades == len(result.trades)

    csv_path = write_trades_csv(result.trades, tmp_path / "trades.csv")
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("symbol,entry_date")
