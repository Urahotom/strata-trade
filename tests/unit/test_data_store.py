"""bars_daily 永続化と出典間突合のテスト（アダプタ同一性・TESTING §3.4）."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from strata.data.base import DailyBar
from strata.data.store import BarRepository
from strata.store import init_db


@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[BarRepository]:
    conn = init_db(tmp_path / "bars.db")
    yield BarRepository(conn)
    conn.close()


def bar(day: str, source: str, close: float, *, symbol: str = "8306", v: int = 100) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        date=day,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=v,
        source=source,  # type: ignore[arg-type]
    )


class TestUpsert:
    def test_保存と取り出しを日付昇順で(self, repo: BarRepository) -> None:
        n = repo.upsert_bars([bar("2026-10-21", "jquants", 105), bar("2026-10-20", "jquants", 100)])
        assert n == 2
        bars = repo.get_bars("8306", "jquants")
        assert [b.date for b in bars] == ["2026-10-20", "2026-10-21"]

    def test_同一キーは上書きで増加しない(self, repo: BarRepository) -> None:
        repo.upsert_bars([bar("2026-10-20", "jquants", 100)])
        repo.upsert_bars([bar("2026-10-20", "jquants", 101)])
        bars = repo.get_bars("8306", "jquants")
        assert len(bars) == 1
        assert bars[0].close == 101

    def test_出典が違えば別レコード(self, repo: BarRepository) -> None:
        repo.upsert_bars([bar("2026-10-20", "jquants", 100), bar("2026-10-20", "yfinance", 100)])
        assert len(repo.get_bars("8306", "jquants")) == 1
        assert len(repo.get_bars("8306", "yfinance")) == 1

    def test_期間絞り込み(self, repo: BarRepository) -> None:
        repo.upsert_bars(
            [bar(d, "jquants", 100) for d in ("2026-10-20", "2026-10-21", "2026-10-22")]
        )
        got = repo.get_bars("8306", "jquants", start="2026-10-21", end="2026-10-21")
        assert [b.date for b in got] == ["2026-10-21"]

    def test_空入力はノーエラー(self, repo: BarRepository) -> None:
        assert repo.upsert_bars([]) == 0

    def test_latest_date(self, repo: BarRepository) -> None:
        repo.upsert_bars([bar(d, "jquants", 100) for d in ("2026-10-20", "2026-10-22")])
        assert repo.latest_date("8306", "jquants") == "2026-10-22"
        assert repo.latest_date("9999", "jquants") is None


class TestCrossCheck:
    def test_一致なら空(self, repo: BarRepository) -> None:
        repo.upsert_bars(
            [bar("2026-10-20", "jquants", 100.0), bar("2026-10-20", "yfinance", 100.0)]
        )
        assert repo.cross_check("8306") == []

    def test_許容差以内は許す(self, repo: BarRepository) -> None:
        repo.upsert_bars(
            [bar("2026-10-20", "jquants", 100.00), bar("2026-10-20", "yfinance", 100.05)]
        )
        assert repo.cross_check("8306", tolerance=0.001) == []

    def test_許容差超過を検出(self, repo: BarRepository) -> None:
        repo.upsert_bars(
            [bar("2026-10-20", "jquants", 100.0), bar("2026-10-20", "yfinance", 110.0)]
        )
        mismatches = repo.cross_check("8306", tolerance=0.001)
        assert len(mismatches) == 1
        assert mismatches[0].date == "2026-10-20"
        assert mismatches[0].rel_diff == pytest.approx(0.0909, abs=1e-3)

    def test_片方にしかない日は突合対象外(self, repo: BarRepository) -> None:
        repo.upsert_bars([bar("2026-10-20", "jquants", 100.0)])
        assert repo.cross_check("8306") == []
