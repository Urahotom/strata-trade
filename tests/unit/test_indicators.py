"""テクニカル指標（indicators.py）の単体テスト."""

from __future__ import annotations

import pytest

from strata.strategy.indicators import (
    bollinger,
    donchian,
    previous_channel_excluding_today,
    rsi,
    sma,
)


class TestSma:
    def test_直近n本の平均(self) -> None:
        assert sma([1, 2, 3, 4], 2) == 3.5

    def test_不足はValueError(self) -> None:
        with pytest.raises(ValueError):
            sma([1], 2)


class TestRsi:
    def test_一本昇りは100(self) -> None:
        assert rsi([float(i) for i in range(20)], 14) == 100.0

    def test_一本下りは0(self) -> None:
        assert rsi([float(100 - i) for i in range(20)], 14) == 0.0

    def test_閾値の範囲(self) -> None:
        closes = [100.0]
        for i in range(30):
            closes.append(closes[-1] * (1.01 if i % 3 else 0.97))
        value = rsi(closes, 14)
        assert 0.0 <= value <= 100.0

    def test_本数不足(self) -> None:
        with pytest.raises(ValueError):
            rsi([1.0, 2.0], 14)


class TestBollinger:
    def test_一定値はバンド幅ゼロ(self) -> None:
        bands = bollinger([5000.0] * 20)
        assert bands.mid == pytest.approx(5000.0)
        assert bands.upper == pytest.approx(5000.0)
        assert bands.lower == pytest.approx(5000.0)

    def test_既知の系列(self) -> None:
        closes = [float(i) for i in range(1, 21)]  # 1..20
        bands = bollinger(closes, 20, 2.0)
        assert bands.mid == pytest.approx(10.5)
        # 母集団標準偏差 sqrt(33.25)
        sd = 33.25**0.5
        assert bands.upper == pytest.approx(10.5 + 2 * sd)
        assert bands.lower == pytest.approx(10.5 - 2 * sd)

    def test_本数不足(self) -> None:
        with pytest.raises(ValueError):
            bollinger([1.0] * 19)


class TestDonchian:
    def test_期間内の高安(self) -> None:
        highs = [10.0, 12.0, 11.0, 13.0]
        lows = [8.0, 9.0, 7.0, 6.0]
        ch = donchian(highs, lows, 2)
        assert ch.upper == 13.0
        assert ch.lower == 6.0

    def test_本日除外の基準(self) -> None:
        highs = [10.0, 12.0, 11.0]
        lows = [8.0, 9.0, 7.0]
        ch = previous_channel_excluding_today(highs, lows, 2)
        assert ch.upper == 12.0  # 本日の11を含まない
        assert ch.lower == 8.0

    def test_長さ不一致(self) -> None:
        with pytest.raises(ValueError):
            donchian([1.0, 2.0], [1.0], 2)
