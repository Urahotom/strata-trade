"""シグナル有効性レポート生成（4戦略を同一相場で比較）.

    uv run python scripts/backtest_report.py                 # 合成相場で即実行
    uv run python scripts/backtest_report.py --real 8306,9984 \
        --history-end 2026-10-23 --cash 3000000              # 実データ（再現キー固定）

出力（既定 var/reports/）:
    trades_<戦略名>.csv   トレード単位台帳（エントリー根拠・損益・保有日数）
    report.md             実験条件（再現キー）＋有効性レポート＋期間分割検証

再現性: yfinance のデータは取得日に依存するため、記録に残す実行は必ず
--history-end を固定する。report.md 冒頭に実験条件カードが出力される。
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

from strata.backtest import (
    BacktestResult,
    BacktestRunner,
    BacktestSettings,
    FillPolicy,
    PeriodRow,
    buy_hold,
    exposure_stats,
    group_stats,
    key_entry_source,
    key_exit_source,
    key_exit_year,
    key_symbol,
    max_drawdown_span,
    render_multi_markdown,
    render_periods_markdown,
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

#: 期間分割検証の最低本数（戦略のウォームアップに必要）
MIN_BARS_PER_PERIOD = 40


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


def real_market(
    symbols: list[str], history_start: str, history_end: str
) -> dict[str, list[DailyBar]]:
    from strata.config import load_settings
    from strata.data import make_provider
    from strata.data.repair import repair_split_jumps

    provider = make_provider(load_settings().data)
    start = date.fromisoformat(history_start)
    end = date.fromisoformat(history_end) if history_end else date.today()
    return {
        symbol: repair_split_jumps(provider.fetch_daily(symbol, start, end)) for symbol in symbols
    }


def strategies() -> list[Strategy]:
    return [
        MovingAverageCross(fast=5, slow=25),
        RSIMeanReversion(period=14, lower=30, upper=70),
        BollingerMeanReversion(period=20, k=2.0),
        DonchianBreakout(entry=20, exit=10),
    ]


def risk_settings() -> RiskSettings:
    # テスト既定（100万円/回）では合成相場の合成価格で機械的に全拒否になるため、
    # レポート検証用の緩いリスク設定を明示的に使う
    return RiskSettings(max_notional_per_order=5_000_000, max_qty_per_order=2_000)


def run_one(
    strategy: Strategy,
    bars: dict[str, list[DailyBar]],
    universe: list[WatchlistEntry],
    cash: float,
) -> BacktestResult:
    runner = BacktestRunner(
        strategy=strategy,
        universe=universe,
        bars=bars,
        risk_settings=risk_settings(),
        settings=BacktestSettings(initial_cash=cash, fill=FillPolicy.NEXT_OPEN),
    )
    return runner.run()


def split_bars(
    bars: dict[str, list[DailyBar]], split: str, before: bool
) -> dict[str, list[DailyBar]]:
    out = {
        symbol: [b for b in series if (b.date < split) == before] for symbol, series in bars.items()
    }
    return {s: v for s, v in out.items() if v}


def period_row(label: str, bars: dict[str, list[DailyBar]], result: BacktestResult) -> PeriodRow:
    dates = sorted({b.date for series in bars.values() for b in series})
    m = result.metrics
    trades = result.trades
    expo = exposure_stats(result.equity_curve)
    wins = sum(1 for t in trades if t.pnl > 0)
    ev = sum(t.pnl for t in trades) / len(trades) if trades else 0.0
    return PeriodRow(
        label=label,
        start=dates[0],
        end=dates[-1],
        total_return=m.get("total_return", 0.0),
        cagr=m.get("cagr", 0.0),
        max_drawdown=m.get("max_drawdown", 0.0),
        trades=len(trades),
        win_rate=(wins / len(trades)) if trades else 0.0,
        expected_value=ev,
        time_in_market=expo.time_in_market,
    )


def conditions_card(args: argparse.Namespace, history_end: str) -> str:
    return "\n".join(
        [
            "# 実験条件（再現キー）",
            "",
            f"- 実行コマンド: `python {' '.join(sys.argv)}`",
            "- データ: "
            f"{'実データ（provider は STRATA_DATA__PROVIDER）' if args.real else '合成相場'}"
            f" / 期間 {args.history_start}〜{history_end}（取得日でデータが変わるため "
            "--history-end 固定が必須）",
            f"- 初期資金: {args.cash:,.0f} 円 / 1トレードコスト C: {args.cost_per_trade:,.0f} 円"
            "（SOR新規発注は手数料無料のため実運用と一致）",
            "- 約定: NEXT_OPEN（翌営業日始値） / 気配近似: 終値ベース",
            "- 戦略パラメータ: MA(5,25) RSI(14,30/70) BB(20,2.0) Donchian(20/10)"
            "（いずれも未調整のデフォルト値。期間分割検証で過剰適合を疑う）",
            f"- 実行日: {date.today().isoformat()}",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="var/reports")
    parser.add_argument("--cash", type=float, default=3_000_000.0)
    parser.add_argument("--real", help="カンマ区切り銘柄で実データ（要 STRATA_DATA__*）")
    parser.add_argument("--history-start", default="2015-01-01")
    parser.add_argument("--history-end", default="", help="YYYY-MM-DD（既定=本日）")
    parser.add_argument(
        "--split",
        default="2021-01-01",
        help="学習・調整期間/最終評価期間の分割日（none で無効）",
    )
    parser.add_argument(
        "--cost-per-trade",
        type=float,
        default=0.0,
        help="1トレード当たりコスト C（SOR新規発注は手数料無料のため既定0）",
    )
    args = parser.parse_args()
    split = None if args.split.lower() == "none" else args.split
    history_end = args.history_end or date.today().isoformat()

    if args.real:
        symbols = [s.strip() for s in args.real.split(",") if s.strip()]
        bars = real_market(symbols, args.history_start, args.history_end)
        universe = [WatchlistEntry(symbol=s, trading_unit=100, stop_width=20) for s in symbols]
    else:
        bars = synthetic_market()
        universe = UNIVERSE

    benchmark = buy_hold(bars, args.cash)

    outdir = Path(args.outdir)
    sections: list[tuple[str, dict[str, object]]] = []
    period_md: list[str] = []
    print(conditions_card(args, history_end).replace("# 実験条件（再現キー）", "実験条件"))
    print(
        f"{'ベンチマーク Buy&Hold':<20}{'':6}{'':8}{'':10}{'':8}"
        f"{benchmark.total_return * 100:>9.2f}%  (DD {benchmark.max_drawdown * 100:,.2f} %)"
    )
    print(f"{'戦略':<22}{'trade':>6}{'勝率':>8}{'期待値':>10}{'PF':>8}{'総return':>10}")
    for strategy in strategies():
        result = run_one(strategy, bars, universe, args.cash)
        trades = result.trades
        csv_path = write_trades_csv(trades, outdir / f"trades_{strategy.name}.csv")
        overall = group_stats(trades, key_entry_source)
        m = result.metrics
        pf = _pf(trades)
        win = sum(1 for t in trades if t.pnl > 0) / len(trades) if trades else 0.0
        ev = sum(t.pnl for t in trades) / len(trades) if trades else 0.0
        excess = m.get("total_return", 0.0) - benchmark.total_return
        span = max_drawdown_span(result.equity_curve)
        expo = exposure_stats(result.equity_curve)
        mdd_text = (
            f"{span.depth * 100:.1f}%({span.peak_date}→{span.trough_date})"
            if span is not None
            else "-"
        )
        print(
            f"{strategy.name:<22}{len(trades):>6}{win * 100:>7.1f}%"
            f"{ev:>10,.0f}{pf:>8.2f}{m.get('total_return', 0.0) * 100:>9.2f}%"
            f"  超過 {excess * 100:+7.2f}pt"
            f"  MDD {mdd_text}  投資時間 {expo.time_in_market * 100:.0f}%"
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
                    "by_year": group_stats(trades, key_exit_year),
                    "benchmark": benchmark,
                    "curve": result.equity_curve,
                    "cost_per_trade": args.cost_per_trade,
                },
            )
        )
        print(f"  -> {csv_path}")

        if split is not None:
            rows: list[PeriodRow] = []
            for label, before in [("学習・調整期間", True), ("最終評価期間", False)]:
                sub = split_bars(bars, split, before=before)
                if not sub or max(len(v) for v in sub.values()) < MIN_BARS_PER_PERIOD:
                    continue
                sub_universe = [u for u in universe if u.symbol in sub]
                rows.append(period_row(label, sub, run_one(strategy, sub, sub_universe, args.cash)))
            if len(rows) == 2:
                block = render_periods_markdown(strategy.name, rows)
                period_md.append(block)
                for row in rows:
                    print(
                        f"    {row.label} {row.start}〜{row.end}: "
                        f"return {row.total_return * 100:>8.2f}% / 年率 {row.cagr * 100:>6.2f}%"
                        f" / MDD {row.max_drawdown * 100:>5.1f}% / E {row.expected_value:>9,.0f}"
                        f" / 投資時間 {row.time_in_market * 100:>5.1f}%"
                    )
    report_md = "\n\n---\n\n".join(
        [conditions_card(args, history_end), render_multi_markdown(sections), *period_md]
    )
    report = write_markdown(report_md, outdir / "report.md")
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
