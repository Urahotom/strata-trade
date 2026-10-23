"""リスクオフ・オーバーレイ vs 素のB&H（1306.T TOPIX ETF）.

F-004 の帰結（常時ロングのシグナル戦略は「ただ持つだけ」に勝てない、ある
とすれば DD と回復期間を買うオーバーレイ）の検証。方針は**古典パラメータ固定**
で、学習期間での最適化を一切行わない。

    $env:STRATA_DATA__PROVIDER = "yfinance"
    uv run python scripts/overlay_vs_buyhold.py
    uv run python scripts/overlay_vs_buyhold.py --cost-rate 0.001   # 片道0.1%感度

出力: var/reports/overlay.md（条件カード＋全期間表＋評価窓表）
"""

from __future__ import annotations

import argparse
import itertools
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_walkforward import fetch_bars

from strata.backtest import (
    exposure_stats,
    ma_cross_weights,
    ma_filter_weights,
    max_drawdown_span,
    simulate_weights,
    vol_target_weights,
)
from strata.backtest.metrics import EquityPoint

TRADING_DAYS = 244


def slice_curve(curve: list[EquityPoint], start: str, end: str) -> list[EquityPoint]:
    return [p for p in curve if start <= p.date < end]


def _span_days(curve: list[EquityPoint]) -> int:
    d0 = date.fromisoformat(curve[0].date)
    d1 = date.fromisoformat(curve[-1].date)
    return (d1 - d0).days


def metrics_of(curve: list[EquityPoint]) -> dict[str, float]:
    """期間スライスの成績（期初評価額=始点比）。"""
    if len(curve) < 2:
        return {}
    ref = curve[0].equity
    if ref <= 0:
        return {}
    total = curve[-1].equity / ref - 1.0
    years = _span_days(curve) / 365.25
    rate = (
        (curve[-1].equity / ref) ** (1.0 / years) - 1.0
        if years > 0 and curve[-1].equity > 0
        else 0.0
    )
    span = max_drawdown_span(curve)
    expo = exposure_stats(curve)
    return {
        "total": total,
        "cagr": rate,
        "mdd": span.depth if span else 0.0,
        "under": float(span.days_under_water) if span else 0.0,
        "recovered": 1.0 if span and span.recovery_date else 0.0,
        "time": expo.time_in_market,
    }


def switches_per_year(weights: list[float], days: int) -> float:
    switches = sum(1 for a, b in itertools.pairwise(weights) if abs(a - b) > 1e-9)
    return switches / max(days / TRADING_DAYS, 1e-9)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="1306.T")
    parser.add_argument("--cache", default="var/reports/p9_bars.json")
    parser.add_argument("--history-start", default="2014-06-01")
    parser.add_argument("--history-end", default="2026-10-23")
    parser.add_argument("--cash", type=float, default=10_000_000.0)
    parser.add_argument("--cost-rate", type=float, default=0.0005, help="片道コスト率")
    parser.add_argument("--eval-years", default="2019,2021,2023,2025")
    parser.add_argument("--outdir", default="var/reports")
    args = parser.parse_args()

    bars = fetch_bars([args.symbol], args.history_start, args.history_end, Path(args.cache))
    series = [b for b in bars[args.symbol] if args.history_start <= b.date <= args.history_end]
    if len(series) < 300:
        print("データが足りません")
        return 1
    dates = [b.date for b in series]
    closes = [b.close for b in series]

    variants: dict[str, list[float]] = {
        "素のB&H": [1.0] * len(closes),
        "200日MAフィルタ": ma_filter_weights(closes, window=200),
        "MA50x200クロス": ma_cross_weights(closes, fast=50, slow=200),
        "ボラ目標20%（20日）": vol_target_weights(closes, window=20, target_annual=0.2),
    }
    curves = {
        name: simulate_weights(dates, closes, w, initial_cash=args.cash, cost_rate=args.cost_rate)
        for name, w in variants.items()
    }
    # 無コスト版も感度比較用
    curves_free = {
        name: simulate_weights(dates, closes, w, initial_cash=args.cash, cost_rate=0.0)
        for name, w in variants.items()
    }

    lines = [
        "# リスクオフ・オーバーレイ vs 素のB&H",
        "",
        f"- 条件: {args.symbol}（TOPIX ETF・配当込み調整済み）{dates[0]}〜{dates[-1]}"
        f" / 資金 {args.cash:,.0f} 円 / 片道コスト {args.cost_rate * 100:.3f} %"
        "（SOR手数料無料＋スリッページ見積）/ 判定=当日終値・執行=当日終値"
        "（翌日から反映）/ パラメータは古典値固定（最適化なし）",
        "",
        "## 全期間",
        "",
        "| 方針 | 総return | 年率 | MDD | 水没日数 | 投資時間 | 切替/年 | vs B&H |",
        "|---|---|---|---|---|---|---|---|",
    ]
    base_full = metrics_of(curves["素のB&H"])
    for name, curve in curves.items():
        m = metrics_of(curve)
        span = max_drawdown_span(curve)
        rec = "回復済" if span and span.recovery_date else "未回復"
        excess = m["total"] - base_full["total"]
        lines.append(
            f"| {name} | {m['total'] * 100:+.1f}% | {m['cagr'] * 100:+.2f}% | "
            f"{m['mdd'] * 100:.1f}%（{rec}） | {m['under']:.0f} | "
            f"{m['time'] * 100:.0f}% | {switches_per_year(variants[name], len(closes)):.1f} | "
            f"{excess * 100:+.1f}pt |"
        )
    print(lines[0])
    for line in lines[1:]:
        print(line)

    lines += [
        "",
        f"## コスト感度（総return: 無コスト → 片道{args.cost_rate * 100:.3f}%）",
        "",
    ]
    for name in variants:
        free = metrics_of(curves_free[name])
        paid = metrics_of(curves[name])
        lines.append(
            f"- {name}: {free['total'] * 100:+.1f}% → {paid['total'] * 100:+.1f}%"
            f"（{(paid['total'] - free['total']) * 100:+.1f}pt）"
        )
    lines.append("")

    lines += ["## 評価窓（年の切り出し。重みは全期間の履歴から計算）", ""]
    eval_years = [int(y) for y in args.eval_years.split(",")]
    for year in eval_years:
        start, end = f"{year}-01-01", f"{year + 1}-01-01"
        lines += [
            f"### 評価 {year}",
            "",
            "| 方針 | return | MDD | 水没 | 投資時間 |",
            "|---|---|---|---|---|",
        ]
        print(f"\n== 評価 {year} ==")
        for name, curve in curves.items():
            m = metrics_of(slice_curve(curve, start, end))
            if not m:
                continue
            row = (
                f"| {name} | {m['total'] * 100:+.1f}% | {m['mdd'] * 100:.1f}% | "
                f"{m['under']:.0f} | {m['time'] * 100:.0f}% |"
            )
            lines.append(row)
            print(
                f"  {name:<18} return {m['total'] * 100:>7.1f}%  MDD {m['mdd'] * 100:>5.1f}%"
                f"  水没 {m['under']:>4.0f}日  投資時間 {m['time'] * 100:>5.1f}%"
            )
        lines.append("")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report = outdir / "overlay.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nレポート: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
