"""walk-forward検証（幅広いちぬき30銘柄＋指数B&H＋期間ロール）.

F-003の教訓（2銘柄・単一期間・パラメータ未調整では判定不能）を潰す:
  1. 銘柄数を30（TOPIX core30風の業種横断の東証プライム大型）に広げる
  2. ベンチマークを等額B&H（同一30銘柄）と TOPIX ETF(1306.T) の2本にする
  3. 学習期間で grid からパラメータを選び、翌年の評価期間だけで採点する
     walk-forward を複数窓でロールする

    $env:STRATA_DATA__PROVIDER = "yfinance"
    uv run python scripts/backtest_walkforward.py            # 本番（要ネット初回）
    uv run python scripts/backtest_walkforward.py --symbols 7203,6758 --eval-years 2023

データは var/reports/p9_bars.json にスナップショットし、再実行時は
ネットワークを使わない（取得日を固定して再現性を担保）。
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from strata.backtest import (
    BacktestRunner,
    BacktestSettings,
    FillPolicy,
    buy_hold,
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

#: TOPIX core30 風の業種横断30銘柄（現在上場の時価総額上位・長期連続データ期待可）
#: ※「今存在する銘柄を選ぶ」生存者バイアスは消せないため前提として明記する
TOPIX30 = [
    "1605",
    "3382",
    "4004",
    "4502",
    "5401",
    "6146",
    "6301",
    "6501",
    "6702",
    "6758",
    "6857",
    "6902",
    "6963",
    "6981",
    "7203",
    "7267",
    "7269",
    "7751",
    "7951",
    "8001",
    "8002",
    "8035",
    "8058",
    "8306",
    "8604",
    "8766",
    "9101",
    "9201",
    "9432",
    "9433",
]

#: 指数B&Hの実働はTOPIX ETF（1306.T）。Yahoo chart API は ^TOPX 指数系列を
#: 返さないため ETF を代用する（配当込み調整済み系列）
INDEX_SYMBOL = "1306.T"


@dataclass(frozen=True)
class Family:
    """パラメータgridとファクトリの組（grid内の1つが学習期間で選択される）."""

    name: str
    configs: list[tuple[float, ...]]
    build: Callable[[tuple[float, ...]], Strategy]


def families() -> list[Family]:
    return [
        Family(
            "ma_cross",
            [(5, 25), (5, 50), (10, 30), (20, 50)],
            lambda c: MovingAverageCross(fast=int(c[0]), slow=int(c[1])),
        ),
        Family(
            "rsi_reversion",
            [(14, 30, 70), (14, 25, 75), (10, 30, 70)],
            lambda c: RSIMeanReversion(period=int(c[0]), lower=int(c[1]), upper=int(c[2])),
        ),
        Family(
            "bollinger_reversion",
            [(20, 2.0), (20, 1.5), (14, 2.0)],
            lambda c: BollingerMeanReversion(period=int(c[0]), k=float(c[1])),
        ),
        Family(
            "donchian_breakout",
            [(20, 10), (40, 15), (55, 20)],
            lambda c: DonchianBreakout(entry=int(c[0]), exit=int(c[1])),
        ),
    ]


def fetch_bars(symbols: list[str], start: str, end: str, cache: Path) -> dict[str, list[DailyBar]]:
    """キャッシュマージ方式のデータ取得（未取得銘柄だけ取得して追記）.

    注意: キャッシュは取得期間を保持しないため、ヒストリー窓を変えて
    再取得したい場合はキャッシュファイルを削除する（研究用スクリプトの約束）。
    """
    raw: dict[str, list[dict]] = {}
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
    missing = [s for s in symbols if s not in raw]
    if missing:
        from strata.config import load_settings
        from strata.data import make_provider

        provider = make_provider(load_settings().data)
        for symbol in missing:
            series = []
            for attempt in range(2):
                try:
                    series = provider.fetch_daily(
                        symbol, date.fromisoformat(start), date.fromisoformat(end)
                    )
                    break
                except Exception as exc:  # 取得不可銘柄は除外して続行
                    if attempt == 0:
                        time.sleep(3.0)
                        continue
                    print(f"  ! {symbol} 取得失敗（除外）: {exc}")
            if not series:
                print(f"  ! {symbol} データなし（除外）")
                continue
            raw[symbol] = [
                {
                    "date": b.date,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in series
            ]
            print(f"  + {symbol} {len(series)}本")
            time.sleep(0.4)  # Yahoo 連続要求の Rate Limit 回避
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        print(f"キャッシュ更新: {cache}（計 {len(raw)}銘柄）")
    else:
        print(f"キャッシュ読込: {cache}（{len(raw)}銘柄）")
    bars = {
        symbol: [DailyBar(symbol=symbol, source="yfinance", **rec) for rec in raw[symbol]]
        for symbol in symbols
        if symbol in raw
    }
    return _load_and_repair(bars)


def _load_and_repair(bars: dict[str, list[DailyBar]]) -> dict[str, list[DailyBar]]:
    """未登録分割・不正ティックを研究用に修復する（F-004 正誤参照）."""
    from strata.data.repair import repair_split_jumps

    return {symbol: repair_split_jumps(series) for symbol, series in bars.items()}


def slice_bars(bars: dict[str, list[DailyBar]], start: str, end: str) -> dict[str, list[DailyBar]]:
    out = {symbol: [b for b in series if start <= b.date < end] for symbol, series in bars.items()}
    return {s: v for s, v in out.items() if len(v) >= 40}


def run(strategy: Strategy, bars: dict[str, list[DailyBar]], cash: float) -> dict[str, float]:
    universe = [WatchlistEntry(symbol=s, trading_unit=100, stop_width=20) for s in bars]
    result = BacktestRunner(
        strategy=strategy,
        universe=universe,
        bars=bars,
        risk_settings=RiskSettings(max_notional_per_order=5_000_000, max_qty_per_order=2_000),
        settings=BacktestSettings(initial_cash=cash, fill=FillPolicy.NEXT_OPEN),
    ).run()
    m = dict(result.metrics)
    m["trades"] = float(len(result.trades))
    m["expected_value"] = (
        sum(t.pnl for t in result.trades) / len(result.trades) if result.trades else 0.0
    )
    return m


def bh_window(bars: dict[str, list[DailyBar]], start: str, end: str, cash: float) -> float:
    """期間内等額B&Hリターン（buy_hold を期間スライスに適用）."""
    result = buy_hold(slice_bars(bars, start, end), cash)
    return result.total_return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="カンマ区切り（既定=TOPIX30プリセット）")
    parser.add_argument(
        "--index", default=INDEX_SYMBOL, help="指数B&H（yahoo raw コード、none で省略）"
    )
    parser.add_argument("--history-start", default="2014-06-01")
    parser.add_argument("--history-end", default="2026-10-23")
    parser.add_argument("--eval-years", default="2019,2021,2023,2025")
    parser.add_argument("--train-years", type=int, default=4)
    parser.add_argument("--cash", type=float, default=10_000_000.0)
    parser.add_argument("--cache", default="var/reports/p9_bars.json")
    parser.add_argument("--outdir", default="var/reports")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else list(TOPIX30)
    eval_years = [int(y) for y in args.eval_years.split(",")]
    index_symbol = None if args.index.lower() == "none" else args.index

    print(f"データ取得/ロード: {len(symbols)}銘柄 + 指数 {index_symbol}")
    wanted = list(dict.fromkeys(symbols + ([index_symbol] if index_symbol else [])))
    all_bars = fetch_bars(wanted, args.history_start, args.history_end, Path(args.cache))
    trade_symbols = [s for s in symbols if s in all_bars]
    index_bars = {s: all_bars[s] for s in ([index_symbol] if index_symbol in all_bars else [])}
    universe_bars = {s: all_bars[s] for s in trade_symbols}
    print(f"対象 {len(trade_symbols)}銘柄 / 学習 {args.train_years}年 / 評価年 {eval_years}")

    outdir = Path(args.outdir)
    lines: list[str] = [
        "# walk-forward 検証（学習期間で grid 選択 → 翌年のみ採点）",
        "",
        f"- 条件: {len(trade_symbols)}銘柄 / 資金 {args.cash:,.0f} 円 / 学習"
        f" {args.train_years}年 / 評価 {eval_years} / データ {args.history_start}〜"
        f"{args.history_end}（キャッシュ {args.cache}）/ C=0（SOR無料）",
        "- 採点対象は評価年のみ。grid選択は学習期間の総リターン最大",
        "",
    ]
    wins: dict[str, int] = {f.name: 0 for f in families()}
    excess_sum: dict[str, float] = {f.name: 0.0 for f in families()}

    for year in eval_years:
        train_start = f"{year - args.train_years}-01-01"
        train_end = f"{year}-01-01"
        eval_end = f"{year + 1}-01-01"
        train = slice_bars(universe_bars, train_start, train_end)
        ev = slice_bars(universe_bars, train_end, eval_end)
        if not ev:
            continue
        bench_bh = bh_window(universe_bars, train_end, eval_end, args.cash)
        bench_index = bh_window(index_bars, train_end, eval_end, args.cash) if index_bars else None
        bench_text = f"{bench_bh * 100:+7.2f}%"
        idx_text = f"{bench_index * 100:+7.2f}%" if bench_index is not None else "      -"
        print(
            f"\n== 評価 {year}（学習 {train_start}〜{train_end}）"
            f" B&H(30) {bench_text}  TOPIX {idx_text} =="
        )
        lines.append(f"## 評価 {year}（学習 {train_start}〜{train_end}）")
        lines.append("")
        lines.append(f"- ベンチマーク: 等額B&H {bench_text} / 指数 {idx_text}")
        lines.append("")
        lines.append(
            "| 戦略 | 選択パラメータ(学習) | 評価return | vs B&H | 評価trade | 期待値/回 |"
        )
        lines.append("|---|---|---|---|---|---|")
        for fam in families():
            best_config = fam.configs[0]
            best_return = float("-inf")
            for config in fam.configs:
                m = run(fam.build(config), train, args.cash)
                if m.get("total_return", 0.0) > best_return:
                    best_return = m.get("total_return", 0.0)
                    best_config = config
            m = run(fam.build(best_config), ev, args.cash)
            ret = m.get("total_return", 0.0)
            excess = ret - bench_bh
            wins[fam.name] += int(excess > 0)
            excess_sum[fam.name] += excess
            print(
                f"  {fam.name:<20} 選択 {best_config} "
                f"return {ret * 100:>7.2f}%  vs B&H {excess * 100:+7.2f}pt "
                f"trades {m['trades']:.0f}  E {m['expected_value']:>9,.0f}"
            )
            lines.append(
                f"| {fam.name} | {best_config}（学習 {best_return * 100:+.1f}%） | "
                f"{ret * 100:+.2f}% | {excess * 100:+.2f}pt | {m['trades']:.0f} | "
                f"{m['expected_value']:,.0f} |"
            )
        lines.append("")

    lines.append("## まとめ（評価年で等額B&Hに勝った回数）")
    lines.append("")
    for fam in families():
        avg_excess = excess_sum[fam.name] / max(len(eval_years), 1)
        lines.append(
            f"- {fam.name}: {wins[fam.name]}/{len(eval_years)} 勝 "
            f"（平均超過 {avg_excess * 100:+.2f}pt）"
        )
    report = outdir / "walkforward.md"
    outdir.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nレポート: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
