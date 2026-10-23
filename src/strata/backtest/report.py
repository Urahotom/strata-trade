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

    @staticmethod
    def empty() -> StatBlock:
        return StatBlock(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def summarize(trades: Sequence[ClosedTrade]) -> StatBlock:
    """閉じたトレード群の集計."""
    if not trades:
        return StatBlock.empty()
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0
    return StatBlock(
        trades=len(trades),
        wins=len(wins),
        win_rate=len(wins) / len(trades),
        total_pnl=sum(pnls),
        avg_pnl=sum(pnls) / len(trades),
        profit_factor=profit_factor,
        expected_value=sum(pnls) / len(trades),
        avg_hold_days=sum(t.hold_days for t in trades) / len(trades),
        max_loss=min(pnls),
        max_gain=max(pnls),
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
) -> str:
    """人が読む有効性レポート（Markdown）を描画する."""
    lines = [f"# バックテスト有効性レポート: {strategy_name}", ""]
    lines += _overview(metrics, trades, benchmark)
    lines += _section("エントリー根拠別（どのシグナルが儲けたか）", by_entry_source)
    lines += _section("エグジット根拠別", by_exit_source)
    lines += _section("銘柄別", by_symbol)
    lines += _year_section(by_year, benchmark)
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
) -> list[str]:
    overall = summarize(trades)
    lines = ["## 概要", ""]
    for label, key in (
        ("総リターン", "total_return"),
        ("最大DD", "max_drawdown"),
        ("win率（約定サマリ）", "win_rate"),
        ("実現損益", "realized_pnl"),
        ("シグナル見送り", "rejected_orders"),
    ):
        if key not in metrics:
            continue
        value = metrics[key]
        if key in ("total_return", "max_drawdown", "win_rate"):
            lines.append(f"- {label}: {value * 100:,.2f} %")
        else:
            lines.append(f"- {label}: {value:,.0f}")
    lines.append(
        f"- トレード（FIFO対応）: {overall.trades} 往復 / "
        f"勝率 {overall.win_rate * 100:,.1f} % / "
        f"期待値 {overall.expected_value:,.0f} 円 / "
        f"PF {_fmt_pf(overall.profit_factor)}"
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
    by_year: Mapping[str, StatBlock] | None, benchmark: BenchmarkResult | None
) -> list[str]:
    """決済年ごとの戦略損益と Buy&Hold 年次リターンの対照表."""
    if not by_year:
        return []
    lines = [
        "## 年別（戦略 vs Buy&Hold）",
        "",
        "| 年 | トレード数 | 勝率 | 戦略 合計損益 | PF | B&Hold 年間リターン |",
        "|---|---|---|---|---|---|",
    ]
    bench_yearly = benchmark.yearly_returns if benchmark is not None else {}
    for year, stat in by_year.items():
        bench = bench_yearly.get(year)
        bench_text = f"{bench * 100:,.1f} %" if bench is not None else "-"
        lines.append(
            f"| {year} | {stat.trades} | {stat.win_rate * 100:,.1f} % | "
            f"{stat.total_pnl:,.0f} | {_fmt_pf(stat.profit_factor)} | {bench_text} |"
        )
    lines.append("")
    return lines


def _fmt_pf(pf: float) -> str:
    if pf == math.inf:
        return "∞"
    return f"{pf:,.2f}"


__all__ = [
    "CSV_COLUMNS",
    "StatBlock",
    "TradeKey",
    "group_stats",
    "key_entry_source",
    "key_exit_source",
    "key_exit_year",
    "key_symbol",
    "render_markdown",
    "render_multi_markdown",
    "summarize",
    "trades_to_rows",
    "write_markdown",
    "write_trades_csv",
]
