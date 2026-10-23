"""日足バリデータのテスト（TESTING §3.4: 异常値・欠損・当日バー）."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from strata.data.base import DailyBar
from strata.data.validate import jst_today, validate_bars


def bar(
    day: str, *, o: float = 100, h: float = 110, lo: float = 90, c: float = 105, v: int = 1000
) -> DailyBar:
    return DailyBar(
        symbol="8306", date=day, open=o, high=h, low=lo, close=c, volume=v, source="manual"
    )


class TestValidateBasis:
    def test_正常バーはすべて採用(self) -> None:
        report = validate_bars([bar("2026-10-21"), bar("2026-10-22")])
        assert len(report.valid) == 2
        assert report.is_clean

    def test_高値が安値を下回るのは却下(self) -> None:
        report = validate_bars([bar("2026-10-21", h=80, lo=120)])
        assert not report.valid
        assert len(report.rejected) == 1
        assert any("高値" in r for r in report.rejected[0].reasons)

    def test_日付形式不正は却下(self) -> None:
        report = validate_bars([bar("2026/10/21")])
        assert report.rejected

    def test_同日重複は初目だけ採用(self) -> None:
        report = validate_bars([bar("2026-10-21"), bar("2026-10-21", c=999)])
        assert len(report.valid) == 1
        assert report.valid[0].close == 105
        assert any("重複" in r for r in report.rejected[0].reasons)

    def test_入力順不同でも日付昇順で返す(self) -> None:
        report = validate_bars([bar("2026-10-22"), bar("2026-10-21")])
        assert [b.date for b in report.valid] == ["2026-10-21", "2026-10-22"]

    def test_NaNはモデル時点で弾く(self) -> None:
        # ge=0 制約により NaN は DailyBar 構築時点で拒否される
        with pytest.raises(ValidationError):
            bar("2026-10-21", c=float("nan"))


class TestWarnings:
    def test_出来高ゼロは警告だが採用(self) -> None:
        report = validate_bars([bar("2026-10-21", v=0)])
        assert len(report.valid) == 1
        assert not report.is_clean
        assert any("出来高0" in w for w in report.warnings)

    def test_長期ギャップは警告(self) -> None:
        report = validate_bars([bar("2026-01-05"), bar("2026-03-10")], gap_suspect_days=7)
        assert any("ギャップ" in w for w in report.warnings)

    def test_連休程度のギャップは警告しない(self) -> None:
        report = validate_bars([bar("2026-10-23"), bar("2026-10-27")], gap_suspect_days=7)
        assert not any("ギャップ" in w for w in report.warnings)


class TestMissingToday:
    def test_当日バーありなら中断しない(self) -> None:
        report = validate_bars([bar("2026-10-21"), bar("2026-10-22")], today=date(2026, 10, 22))
        assert not report.missing_today

    def test_当日バー欠損でmissing_today(self) -> None:
        report = validate_bars([bar("2026-10-20")], today=date(2026, 10, 22))
        assert report.missing_today
        assert report.latest_date == "2026-10-20"

    def test_当日が週末なら直近金曜を期待(self) -> None:
        # 2026-10-24 は土曜。金曜 10-23 のバーがあれば欠損ではない
        report = validate_bars([bar("2026-10-23")], today=date(2026, 10, 24))
        assert not report.missing_today

    def test_today未指定なら欠損判定しない(self) -> None:
        report = validate_bars([bar("2001-01-01")])
        assert not report.missing_today


def test_jst_todayはdateを返す() -> None:
    assert isinstance(jst_today(), date)
