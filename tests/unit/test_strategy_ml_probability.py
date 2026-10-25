"""ML騰落確率戦略（ml_probability.py）の単体テスト.

`ml` extra（numpy / scikit-learn）が未導入の場合はスキップする
（コアのオフラインテストを依存増やしないための設計）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("numpy")

import numpy as np

from strata.data.base import DailyBar
from strata.strategy.base import SignalAction
from strata.strategy.ml_probability import (
    FEATURE_NAMES,
    MLProbabilityStrategy,
    _feature_matrix,
    _row_at,
)

START = date(2023, 1, 1)


def bars_of(symbol: str, closes: list[float]) -> list[DailyBar]:
    d = START
    return [
        DailyBar(
            symbol=symbol,
            date=(d + timedelta(days=i)).isoformat(),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=1000 + i,
            source="manual",
        )
        for i, c in enumerate(closes)
    ]


def test_row_requires_min_history() -> None:
    closes = np.linspace(1000, 1200, 40)
    opens = closes.copy()
    vols = np.full(40, 1000.0)
    row = _row_at(closes, opens, vols, 39)
    assert np.isnan(row).all()  # 必要本数（60）不足は全て NaN


def test_feature_matrix_shapes_and_no_lookahead() -> None:
    closes = [1000.0 + i * 2 for i in range(120)]
    _ordered, feats = _feature_matrix(bars_of("1001", closes))
    assert feats.shape == (120, len(FEATURE_NAMES))
    assert np.isnan(feats[10]).any()  # 暖機期間は NaN
    assert not np.isnan(feats[119]).any()  # 末尾は計算済み


def test_no_signal_before_train_end() -> None:
    closes = [1000.0 + (i % 7) * 5 for i in range(80)]
    bars = {"1001": bars_of("1001", closes)}
    # 学習窓の終端（train_end）より手前なら学習も発注もしない（未来参照防止）
    strat = MLProbabilityStrategy(train_start="2023-01-01", train_end="2099-01-01")
    signals = strat.generate_signals(bars)
    assert strat._fitted is False
    assert signals == []


def test_state_transition_dedupes_same_direction() -> None:
    strat = MLProbabilityStrategy(train_start="2023-01-01", train_end="2023-06-01")
    s1 = strat._decide("1001", p_up=0.9, p_down=0.1)
    s2 = strat._decide("1001", p_up=0.9, p_down=0.1)  # 同方向は抑止
    assert s1 is not None and s1.action is SignalAction.BUY
    assert s2 is None
    s3 = strat._decide("1001", p_up=0.1, p_down=0.9)  # 逆方向へ遷移
    assert s3 is not None and s3.action is SignalAction.SELL


def test_end_to_end_runs_on_synthetic_universe() -> None:
    universe = {
        "1001": bars_of("1001", [1000.0 + 6 * i + 40 * ((-1) ** i) for i in range(260)]),
        "2002": bars_of("2002", [2000.0 + (i % 11) * 20 for i in range(260)]),
    }
    strat = MLProbabilityStrategy(
        train_start="2023-01-01", train_end="2023-11-01", threshold=0.5, seed=7
    )
    out = strat.generate_signals(universe)
    assert isinstance(out, list)
    assert all(s.strength >= 0.0 for s in out)


def test_band_property() -> None:
    strat = MLProbabilityStrategy(train_start="2023-01-01", train_end="2023-06-01", band=0.01)
    assert strat.band == 0.01


def test_invalid_label_lag_raises() -> None:
    with pytest.raises(ValueError):
        MLProbabilityStrategy(train_start="2023-01-01", train_end="2023-06-01", label_lag=2)


def test_label_lag_defaults_to_execution_aligned() -> None:
    strat = MLProbabilityStrategy(train_start="2023-01-01", train_end="2023-06-01")
    assert strat.label_lag == 1  # 既定は t+1→t+2（約定実行可と整合）


def test_boundary_label_leak_fixed() -> None:
    """評価期初（train_end 当日）の終値が学習ラベルに漏れないこと（境界リーク防止）.

    lag=0 なら t=train_end-1 のラベルは close[train_end] を参照してしまう。修正後は
    ラベル対象日が train_end 未満のサンプルだけを使うため、train_end 当日の終値を
    操作しても学習域の予測は不変でなければならない。
    """
    p = 1000.0
    base: list[float] = []
    for i in range(200):
        p *= 1.015 if i % 2 == 0 else 0.983  # 毎日 ±1.5%（band 0.5% を超える上下両ラベル）
        base.append(p)
    train_end = (START + timedelta(days=150)).isoformat()  # index 150 = 評価期初
    variant = list(base)
    variant[150] = base[150] * 1.5  # 評価期初の終値だけ巨大な跳ねに差し替え
    query = (START + timedelta(days=149)).isoformat()  # 学習域・特徴は当日まで

    def prob_at_query(closes: list[float]) -> tuple[float, float]:
        strat = MLProbabilityStrategy(
            train_start="2023-01-01",
            train_end=train_end,
            threshold=0.5,
            seed=11,
            label_lag=0,
        )
        bars = {"1001": bars_of("1001", closes)}
        strat.fit(bars)
        for date_str, p_up, p_down in strat.probs(bars["1001"]):
            if date_str == query:
                return (p_up, p_down)
        raise AssertionError("学習域の予測行が見つからない")

    assert prob_at_query(base) == pytest.approx(prob_at_query(variant))


def test_fit_and_probs_public_api() -> None:
    universe = {
        "1001": bars_of("1001", [1000.0 + 5 * i + 60 * ((-1) ** i) for i in range(200)]),
        "2002": bars_of("2002", [2000.0 + (i % 9) * 30 for i in range(200)]),
    }
    strat = MLProbabilityStrategy(train_start="2023-01-01", train_end="2023-11-01", seed=7)
    strat.fit(universe)  # train_end 以前のみで学習（冪等）
    assert strat._fitted
    probs = strat.probs(universe["1001"])
    assert probs  # 暖機以降の行は確率が出る
    for date_str, p_up, p_down in probs:
        assert 0.0 <= p_up <= 1.0
        assert 0.0 <= p_down <= 1.0
        assert len(date_str) == len("2023-01-01")
    # fit は冪等（再実行で落ちない・行数不変）
    strat.fit(universe)
    assert len(strat.probs(universe["1001"])) == len(probs)
