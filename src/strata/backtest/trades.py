"""約定明細 → トレード（往復単位）再構築.

バックテストの有効性判断には「どのシグナルで入り、どこで出ていくら」が必要。
意図台帳の約定（executions）をFIFOで突き合わせ、閉じたトレード・未完トレードに
分解する。**現物買いのみのため売り先行（空売り）は出現しない前提**で、
不整合な売りは例外にする。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date


@dataclass(frozen=True)
class Fill:
    """約定1件（executions 表の行 + シグナル由来の情報）."""

    traded_at: str
    date: str  # YYYY-MM-DD に正規化済み
    symbol: str
    side: str  # BUY / SELL
    qty: int
    price: float
    #: シグナルを出した戦略名（BacktestRunnerが意図台帳と対応付けて付与）
    source: str = ""
    #: シグナルの根拠文字列
    reason: str = ""


@dataclass(frozen=True)
class ClosedTrade:
    """FIFOで対応付け済みの1往復."""

    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    qty: int
    entry_source: str
    entry_reason: str
    exit_source: str
    exit_reason: str

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return self.exit_price / self.entry_price - 1.0

    @property
    def hold_days(self) -> int:
        """保有暦（カレンダー日数）."""
        entry = date.fromisoformat(self.entry_date)
        exit_ = date.fromisoformat(self.exit_date)
        return (exit_ - entry).days


@dataclass(frozen=True)
class OpenTrade:
    """まだ売られていない買い玉（評価外・数量と取得単価のみ）."""

    symbol: str
    entry_date: str
    entry_price: float
    qty: int
    entry_source: str
    entry_reason: str


def normalize_date(raw: str) -> str:
    """約定時刻を YYYY-MM-DD に正規化する.

    対応: 'YYYY-MM-DD' / 'YYYY-MM-DDTHH:MM:SS...' / 'yyyyMMddHHmmss'（ブローカー形式）
    """
    text = raw.strip()
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    raise ValueError(f"日付として解釈できません: {raw!r}")


def build_trades(fills: Sequence[Fill]) -> tuple[list[ClosedTrade], list[OpenTrade]]:
    """約定を銘柄別FIFOで突き合わせ、閉じたトレードと買い残を返す."""
    lots: dict[str, deque[Fill]] = {}
    closed: list[ClosedTrade] = []
    for fill in fills:
        if fill.side == "SELL":
            queue = lots.get(fill.symbol)
            if not queue:
                raise ValueError(f"対応する買いのない売り約定: {fill.symbol} {fill.date}")
            remaining = fill.qty
            while remaining > 0:
                lot = queue[0]
                take = min(remaining, lot.qty)
                closed.append(
                    ClosedTrade(
                        symbol=fill.symbol,
                        entry_date=lot.date,
                        entry_price=lot.price,
                        exit_date=fill.date,
                        exit_price=fill.price,
                        qty=take,
                        entry_source=lot.source,
                        entry_reason=lot.reason,
                        exit_source=fill.source,
                        exit_reason=fill.reason,
                    )
                )
                remaining -= take
                if take == lot.qty:
                    queue.popleft()
                else:
                    queue[0] = replace(lot, qty=lot.qty - take)
        else:
            lots.setdefault(fill.symbol, deque()).append(fill)
    open_trades = [
        OpenTrade(
            symbol=symbol,
            entry_date=lot.date,
            entry_price=lot.price,
            qty=lot.qty,
            entry_source=lot.source,
            entry_reason=lot.reason,
        )
        for symbol, queue in lots.items()
        for lot in queue
    ]
    closed.sort(key=lambda t: (t.exit_date, t.symbol))
    return closed, open_trades
