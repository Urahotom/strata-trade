"""シグナル有効性レポート生成（4戦略を同一相場で比較）.

    uv run python scripts/backtest_report.py                 # 合成相場で即実行
    uv run python scripts/backtest_report.py --real 8306,9984  # 実データ（要データキー）

出力（既定 var/reports/）:
    trades_<戦略名>.csv   トレード単位台帳（エントリー根拠・損益・保有日数）
    report.md             戦略別有効性レポート（勝率・期待値・PF・根拠別）
"""

from __future__ import annotations

import argparse
import math
from datetime import date, timedelta
from pathlib import Path

from strata.backtest import (
    BacktestRunner,
    BacktestSettings,
    FillPolicy,
    group_stats,
    key_entry_source,
    key_exit_source,
    key_symbol,
    render_multi_markdown,
    write_markdown,
    write_trades_csv,
)
from strata.config import RiskSettings, WatchlistEntry
from strata.data.base import DailyBar
from strata.strategy import (
    BollingerMeanReversion,
    DonchianBreakout,
    MovingAverageCross,
    RSIMeanReversion,
    Strategy,
)

UNIVERSE = [
    WatchlistEntry(symbol="1001", trading_unit=100, stop_width=20),
    WatchlistEntry(symbol="2002", trading_unit=100, stop_width=20),
    WatchlistEntry(symbol="3003", trading_unit=100, stop_width=20),
]


def synth_bars(symbol: str, prices: list[float], start: date = date(2024, 1, 4)) -> list[DailyBar]:
    out: list[DailyBar] = []
    d = start
    for p in prices:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(
            DailyBar(
                symbol=symbol,
                date=d.isoformat(),
                open=p,
                high=p * 1.012,
                low=p * 0.988,
                close=p,
                volume=1_000_000,
                source="manual",
            )
        )
        d += timedelta(days=1)
    return out


def synthetic_market() -> dict[str, list[DailyBar]]:
    n = 240
    # 1001: 押し目つき上昇トレンド（MA/ブレイクに有利な相場）
    up = [5000 + 18 * i + 140 * math.sin(i / 5.0) for i in range(n)]
    # 2002: 往って来りのレンジ（RSI/ボリンジャーに有利な相場）
    rng = [5000 + 420 * math.sin(i / 7.0) for i in range(n)]
    # 2002より振幅の小さいV字回復（全戦略の地力試し）
    v = [5000 - 9.0 * i for i in range(n // 2)] + [
        5000 - 9.0 * (n // 2 - 1) + 11.0 * i for i in range(1, n - n // 2 + 1)
    ]
    return {
        "1001": synth_bars("1001", up),
        "2002": synth_bars("2002", rng),
        "3003": synth_bars("3003", v[:n]),
    }


def real_market(symbols: list[str], history_start: str) -> dict[str, list[DailyBar]]:
    from strata.config import load_settings
    from strata.data import make_provider

    provider = make_provider(load_settings().data)
    start = date.fromisoformat(history_start)
    today = date.today()
    return {symbol: provider.fetch_daily(symbol, start, today) for symbol in symbols}


def strategies() -> list[Strategy]:
    return [
        MovingAverageCross(fast=5, slow=25),
        RSIMeanReversion(period=14, lower=30, upper=70),
        BollingerMeanReversion(period=20, k=2.0),
        DonchianBreakout(entry=20, exit=10),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="var/reports")
    parser.add_argument("--cash", type=float, default=3_000_000.0)
    parser.add_argument("--real", help="カンマ区切り銘柄で実データ（要 STRATA_DATA__*）")
    parser.add_argument("--history-start", default="2023-01-04")
    args = parser.parse_args()

    if args.real:
        symbols = [s.strip() for s in args.real.split(",") if s.strip()]
        bars = real_market(symbols, args.history_start)
        universe = [WatchlistEntry(symbol=s, trading_unit=100, stop_width=20) for s in symbols]
    else:
        bars = synthetic_market()
        universe = UNIVERSE

    # テスト既定（100万円/回）では合成相場の合成価格で機械的に全拒否になるため、
    # レポート検証用の緩いリスク設定を明示的に使う
    risk = RiskSettings(max_notional_per_order=5_000_000, max_qty_per_order=2_000)

    outdir = Path(args.outdir)
    sections: list[tuple[str, dict[str, object]]] = []
    print(f"{'戦略':<22}{'trade':>6}{'勝率':>8}{'期待値':>10}{'PF':>8}{'総return':>10}")
    for strategy in strategies():
        runner = BacktestRunner(
            strategy=strategy,
            universe=universe,
            bars=bars,
            risk_settings=risk,
            settings=BacktestSettings(initial_cash=args.cash, fill=FillPolicy.NEXT_OPEN),
        )
        result = runner.run()
        trades = result.trades
        csv_path = write_trades_csv(trades, outdir / f"trades_{strategy.name}.csv")
        overall = group_stats(trades, key_entry_source)
        m = result.metrics
        pf = _pf(trades)
        win = sum(1 for t in trades if t.pnl > 0) / len(trades) if trades else 0.0
        ev = sum(t.pnl for t in trades) / len(trades) if trades else 0.0
        print(
            f"{strategy.name:<22}{len(trades):>6}{win * 100:>7.1f}%"
            f"{ev:>10,.0f}{pf:>8.2f}{m.get('total_return', 0.0) * 100:>9.2f}%"
        )
        sections.append(
            (
                f"{strategy.name} ({type(strategy).__name__})",
                {
                    "metrics": dict(m),
                    "trades": trades,
                    "open_trades": result.open_trades,
                    "by_entry_source": overall,
                    "by_exit_source": group_stats(trades, key_exit_source),
                    "by_symbol": group_stats(trades, key_symbol),
                },
            )
        )
        print(f"  -> {csv_path}")
    report = write_markdown(render_multi_markdown(sections), outdir / "report.md")
    print(f"\nレポート: {report}")
    return 0


def _pf(trades: list) -> float:  # type: ignore[type-arg]
    profit = sum(t.pnl for t in trades if t.pnl > 0)
    loss = -sum(t.pnl for t in trades if t.pnl <= 0)
    if loss > 0:
        return profit / loss
    return float("inf") if profit > 0 else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
