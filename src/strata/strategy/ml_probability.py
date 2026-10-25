"""機械学習による「翌日騰落確率」戦略（クリーンルーム再実装）.

== ライセンス上の位置づけ（必ず読む） ==
本モジュールは参照実装

    hajime-f / stocktrading  (https://github.com/hajime-f/stocktrading)
    ライセンス: CC BY-NC 4.0 (表示 - 非営利)

が採用している **「テクニカル特徴量から、翌営業日が現在値±閾値以上に動く
確率を教師あり分類で予測し、確率が高い銘柄でロング/決済する」** という
*投資判断の考え方* を参考にした、**独立の実装（クリーンルーム）** です。

- 参照実装のソースコード（TensorFlow / Keras の BiLSTM 構成・学習ループ・
  銘柄サンプリング処理）は **1行もコピーしていません**。
- 元実装の BiLSTM ニューラル網は使わず、決定の骨格（特徴量→翌日±閾値の
  発生確率→閾値超で発注）だけを、本リポジトリの戦略プラグイン規約
  （`Strategy` 継承・発注権限なし・未来参照なし）と、軽い依存
  （scikit-learn の MLP）で置き換えています。
- 著作権で保護されないのは「表現」であり「アイデア/アルゴリズム」では
  ないため、次のいずれかを厳守すること:
    1. 本モジュールは **非営利の実験・研究専用**（CC BY-NC 4.0 の趣旨）。
       実運用・商用リリース・MIT の `main` へのマージは行わない。
    2. 出典・ライセンス・変更の旨を上記で明示（CC BY-NC 4.0 の表示義務）。
- 変更の旨: 特徴量は比率・z値に統一し全銘柄でプール学習、分類器は MLP、
  シグナルは Long/Short ではなく本システムのロングオンリー（決済=SELL）に
  适配、再学習はウィンドウ端日で1回（walk-forward 準拠）。

依存（`numpy`, `scikit-learn`）はオプショナル extra `ml` でのみ導入される。
本モジュールを直接 import しない限りコアは標準ライブラリのみで動作する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

import numpy as np
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

from strata.data.base import DailyBar
from strata.strategy.base import Signal, SignalAction, Strategy

#: 特徴量の行数（_feature_matrix と必ず揃える）
FEATURE_NAMES: tuple[str, ...] = (
    "ret1",
    "ret5",
    "ret20",
    "ma5_ratio",
    "ma20_ratio",
    "ma60_ratio",
    "macd_hist_ratio",
    "rsi14_z",
    "boll_pctb",
    "vol20",
    "gap",
    "intraday",
    "vol_ratio",
)

#: 最も要求本数の長い特徴（ma60）に合わせた最小本数
_MIN_HISTORY = 60


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """EMA（adjust=False 相当）。先頭は値そのものから始める."""
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder 方式 RSI を全末尾基準で系列化（不足期間は NaN）."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    delta = np.diff(closes)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


#: EMA・RSI の計算に用いる後方窓（macd(26)+signal(9) の暖機に十分）
_TAIL = 252


def _row_at(closes: np.ndarray, opens: np.ndarray, vols: np.ndarray, t: int) -> np.ndarray:
    """index t 時点の特徴量1行を「t 以前のみ」で計算する（未来参照なし）.

    計算量を一定に保つため EMA/RSI は直近 `_TAIL` 本からの窓で近似する
    （学習・予測で同一手順を使い、両者の値の整合を保つ）。
    必要本数不足や除算不能は NaN を含む行として返す。
    """
    row = np.full(len(FEATURE_NAMES), np.nan)
    if t < _MIN_HISTORY - 1:
        return row
    start = max(0, t - _TAIL + 1)
    win = closes[start : t + 1]
    o = opens[t]
    c = closes[t]
    if c <= 0 or o <= 0 or win.min() <= 0:
        return row
    ema12 = _ema(win, 12)
    ema26 = _ema(win, 26)
    hist = (ema12 - ema26) - _ema(ema12 - ema26, 9)
    rsi = _rsi(win, 14)
    ma5 = closes[t - 4 : t + 1].mean()
    ma20 = closes[t - 19 : t + 1].mean()
    ma60 = closes[t - 59 : t + 1].mean()
    sd20 = closes[t - 19 : t + 1].std()
    rets1 = np.diff(closes[t - 20 : t + 1]) / closes[t - 20 : t]
    vol_mean = vols[t - 19 : t + 1].mean()
    r = rsi[-1]
    row[:] = [
        c / closes[t - 1] - 1.0,
        c / closes[t - 5] - 1.0,
        c / closes[t - 20] - 1.0,
        c / ma5 - 1.0,
        c / ma20 - 1.0,
        c / ma60 - 1.0,
        hist[-1] / c,
        r / 50.0 - 1.0 if not np.isnan(r) else np.nan,
        (c - ma20) / (4.0 * sd20) if sd20 > 0 else np.nan,
        rets1.std(),
        o / closes[t - 1] - 1.0,
        (c - o) / o,
        vols[t] / vol_mean if vol_mean > 0 else 0.0,
    ]
    return row


def _series_arrays(
    bars: Sequence[DailyBar],
) -> tuple[list[DailyBar], np.ndarray, np.ndarray, np.ndarray]:
    ordered = sorted(bars, key=lambda b: b.date)
    closes = np.array([b.close for b in ordered], dtype=float)
    opens = np.array([b.open for b in ordered], dtype=float)
    vols = np.array([float(b.volume) for b in ordered], dtype=float)
    return ordered, closes, opens, vols


def _feature_matrix(bars: Sequence[DailyBar]) -> tuple[list[DailyBar], np.ndarray]:
    """バー列を時系列順に並べ、各行を特徴量 (n, len(FEATURE_NAMES)) にした行列を返す.

    各行 t は「その日までのデータだけ」で計算。必要本数不足の行は NaN。
    """
    ordered, closes, opens, vols = _series_arrays(bars)
    feats = np.full((len(ordered), len(FEATURE_NAMES)), np.nan)
    for t in range(_MIN_HISTORY - 1, len(ordered)):
        feats[t] = _row_at(closes, opens, vols, t)
    return ordered, feats


class MLProbabilityStrategy(Strategy):
    """先送り可能な翌々日 ±band 騰落の確率を予測して発注する戦略.

    - `train_end`（未満）までのデータだけで2つの分類器（上げる/下げる）を学習
    - 以降の各営業日、最新バーの特徴量から確率を予測し、`threshold` 超で
      BUY（上げる確率優位）/ SELL（下げる確率優位）を状態遷移で1度だけ出す
    - ラベルは `close[t+lag] -> close[t+lag+1]`。既定 `label_lag=1` は
      「シグナル日 t の翌営業日終値でエントリーし、その翌日終値までの変動」で、
      NEXT_OPEN（翌営業日終値約定）の執行タイミングと整合させる。`lag=0` は
      従来の t->t+1 で、予測した変動が約定前に完了してしまう非実行系の対照
    - 学習はラベル対象日まで含めて `train_end` 未満に閉じるため、評価期間は
      アウト・オブ・サンプル（境界リークなし）

    参照: hajime-f/stocktrading (CC BY-NC 4.0) の投資判断の考え方。
    実装は独立（モジュール先頭のライセンス注記を参照）。
    """

    name: ClassVar[str] = "ml_probability"

    def __init__(
        self,
        *,
        train_start: str,
        train_end: str,
        band: float = 0.005,
        threshold: float = 0.7,
        label_lag: int = 1,
        hidden_layer_sizes: tuple[int, ...] = (64,),
        max_iter: int = 300,
        seed: int = 42,
    ) -> None:
        if not 0.0 < band < 0.2:
            raise ValueError("band は 0.0〜0.2（翌日変動幅）の範囲で")
        if not 0.5 <= threshold < 1.0:
            raise ValueError("threshold は 0.5 以上 1.0 未満で")
        if label_lag not in (0, 1):
            raise ValueError("label_lag は 0（t→t+1・非実行可）か 1（t+1→t+2・実行可）")
        self._train_start = train_start
        self._train_end = train_end
        self._band = band
        self._threshold = threshold
        self._lag = label_lag
        self._hidden = hidden_layer_sizes
        self._max_iter = max_iter
        self._seed = seed
        self._scaler: StandardScaler | None = None
        self._up: MLPClassifier | None = None
        self._down: MLPClassifier | None = None
        self._fitted = False
        #: 銘柄直近の発注方向（連続発注を抑える状態遷移）
        self._last_action: dict[str, SignalAction] = {}

    # ------------------------------------------------------------------ #

    def generate_signals(self, bars: Mapping[str, Sequence[DailyBar]]) -> list[Signal]:
        if not bars:
            return []
        latest = self._latest_date(bars)
        if not self._fitted:
            if latest is None or latest < self._train_end:
                return []  # まだ学習窓が揃っていない
            self._fit(bars)
        if latest is None:
            return []
        return self._signals_on(bars, latest)

    # ------------------------------------------------------------------ #
    # 診断用 public API（バックテストの意思決定には使わない・学習窓は train_end まで）

    @property
    def band(self) -> float:
        return self._band

    @property
    def label_lag(self) -> int:
        return self._lag

    def fit(self, bars: Mapping[str, Sequence[DailyBar]]) -> None:
        """`train_end` 以前のみで学習する（冪等）。診断・校正用に公開."""
        if not self._fitted:
            self._fit(bars)

    def probs(self, series: Sequence[DailyBar]) -> list[tuple[str, float, float]]:
        """バー列の各営業日に対する (date, p_up, p_down) を返す（診断・未来参照なし）.

        各行の特徴量は「その日以前」だけで計算し、学習済みモデルで確率を出す。
        呼び出し側は必要なら翌日値から実現ラベルを付与する（これは事後解析）。
        """
        if not self._fitted or self._scaler is None:
            return []
        ordered, x = _feature_matrix(series)
        out: list[tuple[str, float, float]] = []
        for i, row in enumerate(x):
            if np.isnan(row).any():
                continue
            out.append((ordered[i].date, self._prob(self._up, row), self._prob(self._down, row)))
        return out

    # ------------------------------------------------------------------ #

    def _latest_date(self, bars: Mapping[str, Sequence[DailyBar]]) -> str | None:
        dates = [series[-1].date for series in bars.values() if series]
        return max(dates) if dates else None

    def _fit(self, bars: Mapping[str, Sequence[DailyBar]]) -> None:
        rows_x: list[np.ndarray] = []
        up_y: list[int] = []
        down_y: list[int] = []
        for series in bars.values():
            ordered, x = _feature_matrix(series)
            closes = [b.close for b in ordered]
            # ラベルは close[t+lag] → close[t+lag+1] の変動（lag=1 が約定実行可能な整合）
            for t in range(len(ordered) - self._lag - 1):
                d = ordered[t].date
                if not (self._train_start <= d < self._train_end):
                    continue
                #: 境界リーク防止：ラベル対象日（変動の終点）も学習窓内に閉じる
                if ordered[t + self._lag + 1].date >= self._train_end:
                    continue
                row = x[t]
                if np.isnan(row).any():
                    continue
                base = closes[t + self._lag]
                nxt = closes[t + self._lag + 1]
                rows_x.append(row)
                up_y.append(1 if nxt >= base * (1.0 + self._band) else 0)
                down_y.append(1 if nxt <= base * (1.0 - self._band) else 0)
        self._fitted = True  # 学習可否に関わらず再試行しない（以後は予測のみ）
        if len(rows_x) < 50:
            return
        self._fit_models(rows_x, up_y, down_y)

    def _fit_models(self, rows_x: list[np.ndarray], up_y: list[int], down_y: list[int]) -> None:
        x = np.asarray(rows_x, dtype=float)
        self._scaler = StandardScaler().fit(x)
        xs = self._scaler.transform(x)
        self._up = self._train_one(xs, np.asarray(up_y))
        self._down = self._train_one(xs, np.asarray(down_y))

    def _train_one(self, xs: np.ndarray, y: np.ndarray) -> MLPClassifier | None:
        if len(set(y.tolist())) < 2:
            return None  # 片クラスのみでは確率を学習できない
        model = MLPClassifier(
            hidden_layer_sizes=self._hidden,
            max_iter=self._max_iter,
            random_state=self._seed,
        )
        model.fit(xs, y)
        return model

    def _signals_on(self, bars: Mapping[str, Sequence[DailyBar]], day: str) -> list[Signal]:
        if self._scaler is None or (self._up is None and self._down is None):
            return []
        signals: list[Signal] = []
        for symbol, series in bars.items():
            if len(series) < _MIN_HISTORY:
                continue
            ordered, closes, opens, vols = _series_arrays(series)
            if ordered[-1].date != day:
                continue  # 当日バーが無い銘柄は対象外
            row = _row_at(closes, opens, vols, len(ordered) - 1)
            if np.isnan(row).any():
                continue
            p_up = self._prob(self._up, row)
            p_down = self._prob(self._down, row)
            sig = self._decide(symbol, p_up, p_down)
            if sig is not None:
                signals.append(sig)
        return signals

    def _prob(self, model: MLPClassifier | None, row: np.ndarray) -> float:
        if model is None or self._scaler is None:
            return 0.0
        xs = self._scaler.transform(row.reshape(1, -1))
        proba = model.predict_proba(xs)[0]
        classes = list(model.classes_)
        idx = classes.index(1) if 1 in classes else 0
        return float(proba[idx])

    def _decide(self, symbol: str, p_up: float, p_down: float) -> Signal | None:
        if p_up >= self._threshold and p_up > p_down:
            action, cond = SignalAction.BUY, self._last_action.get(symbol) is not SignalAction.BUY
            prob = p_up
        elif p_down >= self._threshold and p_down > p_up:
            action, cond = SignalAction.SELL, self._last_action.get(symbol) is not SignalAction.SELL
            prob = p_down
        else:
            return None
        if not cond:
            return None  # 既に同方向シグナル済み（状態遷抑止）
        self._last_action[symbol] = action
        side = "上" if action is SignalAction.BUY else "下"
        return Signal(
            symbol=symbol,
            action=action,
            reason=f"ML翌日{side}確率 {prob:.2f} (up {p_up:.2f}/down {p_down:.2f})",
            strength=round(prob, 4),
        )
