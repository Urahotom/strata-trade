"""⑨ バックテスト（運用と同じ意図台帳・リスクコードパスを流用）."""

from strata.backtest.broker import FillPolicy, PaperBroker
from strata.backtest.metrics import BacktestMetrics, EquityPoint, compute_metrics, max_drawdown
from strata.backtest.report import (
    StatBlock,
    group_stats,
    key_entry_source,
    key_exit_source,
    key_symbol,
    render_markdown,
    render_multi_markdown,
    summarize,
    write_markdown,
    write_trades_csv,
)
from strata.backtest.runner import BacktestResult, BacktestRunner, BacktestSettings
from strata.backtest.trades import (
    ClosedTrade,
    Fill,
    OpenTrade,
    build_trades,
    normalize_date,
)

__all__ = [
    "BacktestMetrics",
    "BacktestResult",
    "BacktestRunner",
    "BacktestSettings",
    "ClosedTrade",
    "EquityPoint",
    "Fill",
    "FillPolicy",
    "OpenTrade",
    "PaperBroker",
    "StatBlock",
    "build_trades",
    "compute_metrics",
    "group_stats",
    "key_entry_source",
    "key_exit_source",
    "key_symbol",
    "max_drawdown",
    "normalize_date",
    "render_markdown",
    "render_multi_markdown",
    "summarize",
    "write_markdown",
    "write_trades_csv",
]
