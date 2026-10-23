"""バックテスト有効性レポート（トレード単位CSV + 集計Markdown）.

「総合リターン」ではなく **エントリー根拠（戦略）・銘柄・エグジット根拠別の
勝率・期待値・プロフィットファクタ** を出し、シグナルの有効性を
判断できるようにする（DESIGN §1 段階的実運用の判断材料）。
"""

from __future__ import annotations

import csv
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from strata.backtest.benchmark import BenchmarkResult
from strata.backtest.metrics import (
    DrawdownSpan,
    EquityPoint,
    exposure_stats,
    max_drawdown_span,
    yearly_exposure,
    yearly_returns,
)
from strata.backtest.trades import ClosedTrade, OpenTrade

#: 集計のグルーピングキー
TradeKey = Callable[[ClosedTrade], str]


def key_entry_source(t: ClosedTrade) -> str:
    return t.entry_source or "(不明)"


def key_exit_source(t: ClosedTrade) -> str:
    return t.exit_source or "(不明)"


def key_symbol(t: ClosedTrade) -> str:
    return t.symbol


def key_exit_year(t: ClosedTrade) -> str:
    """決済年でグループ化する（yearly セクション用）."""
    return t.exit_date[:4]


@dataclass(frozen=True)
class StatBlock:
    """トレード群の有効性サマリ."""

    trades: int
    wins: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    #: 総利益/総損失（損失ゼロで利益ありは inf）
    profit_factor: float
    #: 1トレードあたり期待値
    expected_value: float
    avg_hold_days: float
    max_loss: float
    max_gain: float
    #: 勝ちトレードの平均利益（勝ちなしは 0）
    avg_profit: float
    #: 負けトレードの平均損失の絶対値（負けなしは 0）
    avg_loss: float
    #: 勝ちトレードの平均リターン（買付金額比、0.1=+10%）
    avg_profit_pct: float
    #: 負けトレードの平均損失率の絶対値
    avg_loss_pct: float

    @staticmethod
    def empty() -> StatBlock:
        return StatBlock(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def summarize(trades: Sequence[ClosedTrade]) -> StatBlock:
    """閉じたトレード群の集計."""
    if not trades:
        return StatBlock.empty()
    pnls = [t.pnl for t in trades]
    winning = [t for t in trades if t.pnl > 0]
    losing = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in winning)
    gross_loss = -sum(t.pnl for t in losing)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0
    return StatBlock(
        trades=len(trades),
        wins=len(winning),
        win_rate=len(winning) / len(trades),
        total_pnl=sum(pnls),
        avg_pnl=sum(pnls) / len(trades),
        profit_factor=profit_factor,
        expected_value=sum(pnls) / len(trades),
        avg_hold_days=sum(t.hold_days for t in trades) / len(trades),
        max_loss=min(pnls),
        max_gain=max(pnls),
        avg_profit=(gross_profit / len(winning)) if winning else 0.0,
        avg_loss=(gross_loss / len(losing)) if losing else 0.0,
        avg_profit_pct=(sum(t.return_pct for t in winning) / len(winning)) if winning else 0.0,
        avg_loss_pct=(abs(sum(t.return_pct for t in losing)) / len(losing)) if losing else 0.0,
    )


def group_stats(trades: Sequence[ClosedTrade], key: TradeKey) -> dict[str, StatBlock]:
    """キー別の有効性."""
    buckets: dict[str, list[ClosedTrade]] = {}
    for trade in trades:
        buckets.setdefault(key(trade), []).append(trade)
    return {name: summarize(items) for name, items in sorted(buckets.items())}


CSV_COLUMNS: tuple[str, ...] = (
    "symbol",
    "entry_date",
    "entry_price",
    "exit_date",
    "exit_price",
    "qty",
    "pnl",
    "return_pct",
    "hold_days",
    "entry_source",
    "entry_reason",
    "exit_source",
    "exit_reason",
)


def trades_to_rows(trades: Sequence[ClosedTrade]) -> list[dict[str, object]]:
    """CSV行データ（人が読める形に丸める）."""
    return [
        {
            "symbol": t.symbol,
            "entry_date": t.entry_date,
            "entry_price": round(t.entry_price, 3),
            "exit_date": t.exit_date,
            "exit_price": round(t.exit_price, 3),
            "qty": t.qty,
            "pnl": round(t.pnl, 0),
            "return_pct": round(t.return_pct * 100, 2),
            "hold_days": t.hold_days,
            "entry_source": t.entry_source,
            "entry_reason": t.entry_reason,
            "exit_source": t.exit_source,
            "exit_reason": t.exit_reason,
        }
        for t in trades
    ]


def write_trades_csv(trades: Sequence[ClosedTrade], path: Path) -> Path:
    """トレード単位CSVを書き出す（UTF-8・ヘッダー付き）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(trades_to_rows(trades))
    return path


def render_markdown(
    *,
    strategy_name: str,
    metrics: Mapping[str, float],
    trades: Sequence[ClosedTrade],
    open_trades: Sequence[OpenTrade] = (),
    by_entry_source: Mapping[str, StatBlock] | None = None,
    by_exit_source: Mapping[str, StatBlock] | None = None,
    by_symbol: Mapping[str, StatBlock] | None = None,
    by_year: Mapping[str, StatBlock] | None = None,
    benchmark: BenchmarkResult | None = None,
    curve: Sequence[EquityPoint] = (),
    cost_per_trade: float = 0.0,
) -> str:
    """人が読む有効性レポート（Markdown）を描画する.

    curve は日次評価額（含み損込み）のエクイティ曲線。MDD区間と
    市場投資時間の算出に使う。cost_per_trade は1トレード当たりコスト C
    （SOR新規発注は手数料無料のため既定0。スリッページを見積もるなら設定）。
    """
    lines = [f"# バックテスト有効性レポート: {strategy_name}", ""]
    lines += _overview(metrics, trades, benchmark, cost_per_trade)
    lines += _drawdown_section(curve, benchmark)
    lines += _section("エントリー根拠別（どのシグナルが儲けたか）", by_entry_source)
    lines += _section("エグジット根拠別", by_exit_source)
    lines += _section("銘柄別", by_symbol)
    strategy_yearly: Mapping[str, float] | None = None
    if curve:
        baseline = metrics.get("initial_cash", curve[0].equity)
        strategy_yearly = yearly_returns(curve, baseline)
    lines += _year_section(by_year, benchmark, strategy_yearly)
    lines += _exposure_section(curve)
    if open_trades:
        lines += ["## 未決済の買い玉", ""]
        for lot in open_trades:
            lines.append(
                f"- {lot.symbol} {lot.qty}株 @{lot.entry_price:.1f} "
                f"({lot.entry_date} / {lot.entry_source})"
            )
        lines.append("")
    return "\n".join(lines)


def render_multi_markdown(
    sections: Sequence[tuple[str, Mapping[str, object]]],
) -> str:
    """複数戦略のレポートを1ファイルに結合する."""
    parts = [
        render_markdown(strategy_name=name, **kwargs)  # type: ignore[arg-type]
        for name, kwargs in sections
    ]
    return "\n\n---\n\n".join(parts)


def write_markdown(markdown: str, path: Path) -> Path:
    """Markdownレポートを書き出す."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def _overview(
    metrics: Mapping[str, float],
    trades: Sequence[ClosedTrade],
    benchmark: BenchmarkResult | None = None,
    cost_per_trade: float = 0.0,
) -> list[str]:
    overall = summarize(trades)
    lines = ["## 概要", ""]
    for label, key in (
        ("総リターン", "total_return"),
        ("年率リターン（CAGR）", "cagr"),
        ("最大DD（日次評価額・含み損込み）", "max_drawdown"),
        ("win率（約定サマリ）", "win_rate"),
        ("実現損益", "realized_pnl"),
        ("シグナル見送り", "rejected_orders"),
    ):
        if key not in metrics:
            continue
        value = metrics[key]
        if key in ("total_return", "max_drawdown", "win_rate", "cagr"):
            lines.append(f"- {label}: {value * 100:,.2f} %")
        else:
            lines.append(f"- {label}: {value:,.0f}")
    lines.append(
        f"- トレード（FIFO対応）: {overall.trades} 往復 / "
        f"勝率 {overall.win_rate * 100:,.1f} % / "
        f"期待値 {overall.expected_value:,.0f} 円 / "
        f"PF {_fmt_pf(overall.profit_factor)}"
    )
    lines.append(
        f"- 平均利益 {overall.avg_profit:,.0f} 円（{overall.avg_profit_pct * 100:+,.2f} %） / "
        f"平均損失 {overall.avg_loss:,.0f} 円（−{overall.avg_loss_pct * 100:,.2f} %）"
        "（%は買付金額比）"
    )
    p = overall.win_rate
    e_net = overall.expected_value - cost_per_trade
    lines.append(
        f"- 期待値分解 E = p・W − (1−p)・L − C = "
        f"{p:.2f}×{overall.avg_profit:,.0f} − "
        f"{1 - p:.2f}×{overall.avg_loss:,.0f} − {cost_per_trade:,.0f} "
        f"= **{e_net:,.0f} 円/回**（C=1トレード当たりコスト。SOR新規発注は"
        f"手数料無料、C=0 のときはスリッページ未計上）"
    )
    if "total_return" in metrics and benchmark is not None:
        excess = metrics["total_return"] - benchmark.total_return
        lines.append(
            f"- ベンチマーク Buy&Hold（同一資金・均等・期間内）: "
            f"{benchmark.total_return * 100:,.2f} % / "
            f"DD {benchmark.max_drawdown * 100:,.2f} % / "
            f"**超過 {excess * 100:+,.2f} pt**"
        )
    lines.append("")
    return lines


def _drawdown_section(curve: Sequence[EquityPoint], benchmark: BenchmarkResult | None) -> list[str]:
    """最大DDの発生区間（日次評価額・含み損込み）."""
    if not curve:
        return []
    span = max_drawdown_span(curve)
    if span is None:
        return []
    lines = [
        "## 最大ドローダウン（日次評価額・保有中の含み損込み）",
        "",
        f"- 戦略: **{span.depth * 100:,.2f} %**（ピーク {span.peak_date} → 底 "
        f"{span.trough_date} / {_recovery_text(span)}）",
    ]
    if benchmark is not None and benchmark.curve:
        bench_span = max_drawdown_span(benchmark.curve)
        if bench_span is not None:
            lines.append(
                f"- Buy&Hold: {bench_span.depth * 100:,.2f} %（ピーク "
                f"{bench_span.peak_date} → 底 {bench_span.trough_date} / "
                f"{_recovery_text(bench_span)}）"
            )
    lines.append("")
    return lines


def _recovery_text(span: DrawdownSpan) -> str:
    if span.recovery_date is not None:
        return f"回復 {span.recovery_date}・水没 {span.days_under_water}日"
    return f"期間末までに未回復（水没 {span.days_under_water}日）"


def _section(title: str, groups: Mapping[str, StatBlock] | None) -> list[str]:
    if not groups:
        return []
    lines = [
        f"## {title}",
        "",
        "| 区分 | トレード数 | 勝率 | 合計損益 | 期待値/回 | PF | 平均保有(日) | 最大 loss |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, stat in groups.items():
        lines.append(
            f"| {name} | {stat.trades} | {stat.win_rate * 100:,.1f} % | "
            f"{stat.total_pnl:,.0f} | {stat.expected_value:,.0f} | "
            f"{_fmt_pf(stat.profit_factor)} | {stat.avg_hold_days:,.1f} | "
            f"{stat.max_loss:,.0f} |"
        )
    lines.append("")
    return lines


def _year_section(
    by_year: Mapping[str, StatBlock] | None,
    benchmark: BenchmarkResult | None,
    strategy_yearly: Mapping[str, float] | None = None,
) -> list[str]:
    """決済年ごとの戦略損益と 戦略/B&Hold の年間リターンの対照表."""
    if not by_year and not strategy_yearly:
        return []
    lines = [
        "## 年別（戦略 vs Buy&Hold）",
        "",
        "| 年 | トレード数 | 勝率 | 戦略 合計損益 | PF | 戦略 年間リターン | B&Hold 年間リターン |",
        "|---|---|---|---|---|---|---|",
    ]
    bench_yearly = benchmark.yearly_returns if benchmark is not None else {}
    years = sorted(set(by_year or {}) | set(strategy_yearly or {}))
    for year in years:
        stat = (by_year or {}).get(year)
        trades_text = f"{stat.trades}" if stat is not None else "0"
        win_text = f"{stat.win_rate * 100:,.1f} %" if stat is not None else "-"
        pnl_text = f"{stat.total_pnl:,.0f}" if stat is not None else "-"
        pf_text = _fmt_pf(stat.profit_factor) if stat is not None else "-"
        strat = (strategy_yearly or {}).get(year)
        strat_text = f"{strat * 100:,.1f} %" if strat is not None else "-"
        bench = bench_yearly.get(year)
        bench_text = f"{bench * 100:,.1f} %" if bench is not None else "-"
        lines.append(
            f"| {year} | {trades_text} | {win_text} | {pnl_text} | {pf_text} | "
            f"{strat_text} | {bench_text} |"
        )
    lines.append("")
    return lines


def _exposure_section(curve: Sequence[EquityPoint]) -> list[str]:
    """市場への投資時間（上昇相場で現金を持ちすぎたか等の判別）."""
    if not curve:
        return []
    overall = exposure_stats(curve)
    lines = [
        "## 市場への投資時間（エクスポージャー）",
        "",
        f"- 投資時間率: **{overall.time_in_market * 100:,.1f} %**"
        f"（{overall.market_days}/{overall.days} 営業日で保有） / "
        f"平均エクスポージャー {overall.avg_exposure * 100:,.1f} % / "
        f"最大 {overall.max_exposure * 100:,.1f} %",
        "",
        "| 年 | 投資時間率 | 平均エクスポージャー |",
        "|---|---|---|",
    ]
    for year, stat in yearly_exposure(curve).items():
        lines.append(
            f"| {year} | {stat.time_in_market * 100:,.1f} % | {stat.avg_exposure * 100:,.1f} % |"
        )
    lines.append("")
    return lines


def _fmt_pf(pf: float) -> str:
    if pf == math.inf:
        return "∞"
    return f"{pf:,.2f}"


@dataclass(frozen=True)
class PeriodRow:
    """期間分割検証の1期間ぶんサマリ（学習/調整期間と最終評価期間の対照用）."""

    label: str
    start: str
    end: str
    total_return: float
    cagr: float
    max_drawdown: float
    trades: int
    win_rate: float
    expected_value: float
    time_in_market: float


def render_periods_markdown(strategy_name: str, rows: Sequence[PeriodRow]) -> str:
    """学習/調整期間と最終評価期間を並べた対照テーブル（walk-forward の最小形）.

    「学習・調整に使った期間で良い成績」は当然として落ちており、
    採点対象は **最終評価期間のみ** である（DESIGN §3.9・FINDINGS）。
    """
    lines = [f"# 期間分割検証: {strategy_name}", ""]
    for row in rows:
        mark = "（採点対象）" if row.label == "最終評価期間" else "（参考・調整用）"
        lines.append(f"## {row.label}: {row.start}〜{row.end} {mark}")
        lines.append("")
        lines.append(
            f"- 総リターン {row.total_return * 100:,.2f} % / "
            f"年率 {row.cagr * 100:,.2f} % / 最大DD {row.max_drawdown * 100:,.2f} % / "
            f"投資時間 {row.time_in_market * 100:,.1f} %"
        )
        lines.append(
            f"- トレード {row.trades} 往復 / 勝率 {row.win_rate * 100:,.1f} % / "
            f"期待値 {row.expected_value:,.0f} 円/回"
        )
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "CSV_COLUMNS",
    "PeriodRow",
    "StatBlock",
    "TradeKey",
    "exposure_stats",
    "group_stats",
    "key_entry_source",
    "key_exit_source",
    "key_exit_year",
    "key_symbol",
    "max_drawdown_span",
    "render_markdown",
    "render_multi_markdown",
    "render_periods_markdown",
    "summarize",
    "trades_to_rows",
    "write_markdown",
    "write_trades_csv",
    "yearly_exposure",
]
