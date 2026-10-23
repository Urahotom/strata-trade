"""日足バーの永続化と、出典間突合（アダプタ同一性チェック）."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from strata.data.base import BarSource, DailyBar


@dataclass(frozen=True)
class CloseMismatch:
    """出典間で終値が許容差を超えて食い違った日付."""

    date: str
    close_a: float
    close_b: float
    rel_diff: float


class BarRepository:
    """bars_daily のリポジトリ（`INSERT ... ON CONFLICT` で冪等Upsert）."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_bars(self, bars: Sequence[DailyBar]) -> int:
        """バーをUpsertし、処理件数を返す（同一データ再取込で増えない）."""
        if not bars:
            return 0
        with self._conn:
            self._conn.executemany(
                "INSERT INTO bars_daily (symbol, date, open, high, low, close, volume, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (symbol, date, source) DO UPDATE SET"
                " open = excluded.open, high = excluded.high, low = excluded.low,"
                " close = excluded.close, volume = excluded.volume",
                [
                    (b.symbol, b.date, b.open, b.high, b.low, b.close, b.volume, b.source)
                    for b in bars
                ],
            )
        return len(bars)

    def get_bars(
        self,
        symbol: str,
        source: BarSource,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[DailyBar]:
        sql = "SELECT * FROM bars_daily WHERE symbol = ? AND source = ?"
        params: list[object] = [symbol, source]
        if start is not None:
            sql += " AND date >= ?"
            params.append(str(start))
        if end is not None:
            sql += " AND date <= ?"
            params.append(str(end))
        sql += " ORDER BY date"
        cur = self._conn.execute(sql, params)
        return [
            DailyBar(
                symbol=row["symbol"],
                date=row["date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                source=row["source"],
            )
            for row in cur.fetchall()
        ]

    def latest_date(self, symbol: str, source: BarSource) -> str | None:
        row = self._conn.execute(
            "SELECT MAX(date) AS d FROM bars_daily WHERE symbol = ? AND source = ?",
            (symbol, source),
        ).fetchone()
        return str(row["d"]) if row is not None and row["d"] is not None else None

    def cross_check(
        self,
        symbol: str,
        source_a: BarSource = "jquants",
        source_b: BarSource = "yfinance",
        *,
        start: date | str | None = None,
        end: date | str | None = None,
        tolerance: float = 0.001,
    ) -> list[CloseMismatch]:
        """同一銘柄・同一日の終値を出典間で突合し、許容差超過を列挙する.

        片方にしかない日付は「欠損」ではなく突合対象外（欠損検知は
        バリデータ／エンジン側の責務）。
        """
        sql = (
            "SELECT a.date AS date, a.close AS close_a, b.close AS close_b"
            " FROM bars_daily a JOIN bars_daily b"
            " ON a.symbol = b.symbol AND a.date = b.date"
            " WHERE a.symbol = ? AND a.source = ? AND b.source = ?"
        )
        params: list[object] = [symbol, source_a, source_b]
        if start is not None:
            sql += " AND a.date >= ?"
            params.append(str(start))
        if end is not None:
            sql += " AND a.date <= ?"
            params.append(str(end))
        sql += " ORDER BY a.date"
        mismatches: list[CloseMismatch] = []
        for row in self._conn.execute(sql, params):
            close_a: float = row["close_a"]
            close_b: float = row["close_b"]
            base = max(abs(close_a), abs(close_b))
            rel = 0.0 if base == 0 else abs(close_a - close_b) / base
            if rel > tolerance:
                mismatches.append(
                    CloseMismatch(
                        date=str(row["date"]),
                        close_a=close_a,
                        close_b=close_b,
                        rel_diff=rel,
                    )
                )
        return mismatches
