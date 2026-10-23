"""データ修復（repair.py）のテスト."""

from __future__ import annotations

import pytest

from strata.data.base import DailyBar
from strata.data.repair import repair_split_jumps


def bar(day: str, close: float) -> DailyBar:
    return DailyBar(
        symbol="1306",
        date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
        source="manual",
    )


def days(n: int) -> list[str]:
    from datetime import date, timedelta

    d = date(2024, 1, 1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def closes(prices: list[float]) -> list[DailyBar]:
    ds = days(len(prices))
    return [bar(day, p) for day, p in zip(ds, prices, strict=True)]


def test_通常系列はそのまま() -> None:
    prices = [100.0, 101.0, 99.0, 102.0, 103.0, 101.5, 104.0, 105.0]
    out = repair_split_jumps(closes(prices))
    assert [b.close for b in out] == prices


def test_未登録分割は履歴を再スケール() -> None:
    prices = [100.0, 101.0, 102.0] + [102.0 * 0.1 * (1 + 0.001 * i) for i in range(8)]
    out = repair_split_jumps(closes(prices))
    repaired = [b.close for b in out]
    # 分割日より前が 1/10 になり、連続になる
    assert repaired[0] == pytest.approx(10.0)
    assert repaired[2] == pytest.approx(10.2)
    assert abs(repaired[3] / repaired[2] - 1.0) < 0.02


def test_一過性の不正ティックは前日値で置換() -> None:
    # 2日間だけ 1/10 レベルの応答が混入し、その後元のレベルへ復帰（2026-03 の実データ型）
    prices = [380.0, 375.0, 37.0, 36.5, 381.0, 383.0, 382.0, 384.0]
    out = repair_split_jumps(closes(prices))
    repaired = [b.close for b in out]
    assert repaired[2] == 375.0
    assert repaired[3] == 375.0
    # 復帰日も連続（約1%の変動扱いでそのまま）
    assert abs(repaired[4] / repaired[3] - 1.0) < 0.05
    # 以降は無変更
    assert repaired[5] == 383.0


def test_空は空() -> None:
    assert repair_split_jumps([]) == []
