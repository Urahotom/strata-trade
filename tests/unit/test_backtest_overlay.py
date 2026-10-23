"""リスクオフ・オーバーレイ（overlay.py）のテスト."""

from __future__ import annotations

import pytest

from strata.backtest import (
    ma_cross_weights,
    ma_filter_weights,
    simulate_weights,
    vol_target_weights,
)


class TestMaFilter:
    def test_平均の上で1下で0(self) -> None:
        closes = [100.0] * 200 + [120.0] * 50
        w = ma_filter_weights(closes, window=200)
        assert len(w) == 250
        assert w[198] == 0.0  # 本数不足は現金
        assert w[199] == 0.0  # 終値=平均は「下」
        assert w[200] == 1.0  # 120 > 直近200本平均
        assert all(x == 1.0 for x in w[200:])

    def test_暴落で0に転じる(self) -> None:
        closes = [100.0 + i for i in range(250)] + [50.0] * 10
        w = ma_filter_weights(closes, window=200)
        assert w[249] == 1.0
        assert all(x == 0.0 for x in w[250:])


class TestMaCross:
    def test_逆転で1(self) -> None:
        closes = [100.0] * 250 + [101.0 + i for i in range(60)]
        w = ma_cross_weights(closes, fast=50, slow=200)
        assert w[198] == 0.0  # スロー本数不足は現金
        assert sum(w[250:]) > 0  # 上昇局面でファストがスローを上抜く
        assert w[-1] == 1.0

    def test_fastがslow以上はエラー(self) -> None:
        with pytest.raises(ValueError):
            ma_cross_weights([1.0, 2.0], fast=5, slow=5)


class TestVolTarget:
    def test_一定価格はフル投資(self) -> None:
        w = vol_target_weights([100.0] * 50, window=20, target_annual=0.2)
        assert w[-1] == 1.0  # vol=0 → 1

    def test_高ボラは比率どおりに減倉(self) -> None:
        # 毎日 ±20% ずつ乱高下する系列: 年率ボラは目標を大きく超える
        closes = [100.0]
        for i in range(50):
            closes.append(closes[-1] * (1.2 if i % 2 == 0 else 0.8))
        w = vol_target_weights(closes, window=20, target_annual=0.2)
        assert 0.0 < w[-1] < 0.2
        assert w[19] == 0.0  # 本数不足（リターン19本）は現金


class TestSimulate:
    def test_フル投資は無コストならB_and_H(self) -> None:
        dates = [f"2024-01-{i + 1:02d}" for i in range(3)]
        closes = [100.0, 110.0, 121.0]
        w = [1.0, 1.0, 1.0]
        curve = simulate_weights(dates, closes, w, initial_cash=1_000.0)
        assert curve[-1].equity == pytest.approx(1_210.0)
        assert curve[-1].cash == 0.0

    def test_撤退日は前日までの重みが効く(self) -> None:
        # 2日目に急落するが、重みは2日目終値で0に下ろすだけ（1日目設定=1）
        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        closes = [100.0, 50.0, 50.0]
        w = [1.0, 0.0, 0.0]
        curve = simulate_weights(dates, closes, w, initial_cash=1_000.0)
        assert curve[1].equity == pytest.approx(500.0)  # 急落は被る（当日終値判定）
        assert curve[2].equity == pytest.approx(500.0)  # 撤退後は変動なし

    def test_切替コスト(self) -> None:
        dates = ["2024-01-01", "2024-01-02"]
        closes = [100.0, 100.0]
        w = [1.0, 0.0]
        curve = simulate_weights(dates, closes, w, initial_cash=1_000.0, cost_rate=0.001)
        # 初日 1.0 への切込 0.1% → 999、撤退日さらに 0.1% → 998.001
        assert curve[0].equity == pytest.approx(999.0)
        assert curve[1].equity == pytest.approx(998.001)

    def test_長さ不一致はエラー(self) -> None:
        with pytest.raises(ValueError):
            simulate_weights(["2024-01-01"], [1.0, 2.0], [1.0], initial_cash=1.0)
