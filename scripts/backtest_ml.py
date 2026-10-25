"""ML騰落確率戦略の walk-forward バックテスト（P9キャッシュ再利用）.

参照: hajime-f/stocktrading (CC BY-NC 4.0) の投資判断の考え方を、
      `strata.strategy.ml_probability.MLProbabilityStrategy` でクリーンルーム
      再実装したもの。**非営利の実験・研究専用**（ライセンス注記を参照）。

    $env:UV_PROJECT_ENVIRONMENT = "..."; uv run --extra ml python scripts/backtest_ml.py
    uv run --extra ml python scripts/backtest_ml.py --eval-years 2019,2021,2023,2025

方針:
  - 各評価年に `train_years` 年だけ学習（train_end=評価年始）し、評価年は完全な
    アウト・オブ・サンプルで採点する（未来参照なし）
  - 成績は **評価期間だけで** 集計する（学習期間のフラット期間で希釈しない）
  - 監査: 実現損益と含み損益を分離／投資時間(在市場日割)と平均エクスポージャー
    （資金投入率）を分けて出す／期末の未決済建玉を明示
  - 対照: 同一期間の等額 Buy&Hold と、同エクスポ帯域のランダム売買（選択スキル分離）
  - 校正: 予測確率と実際の発生率を評価期間で突き合わせる（事後解析・意思決定に使わない）

出力: var/reports/ml.md
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from strata.backtest import BacktestRunner, BacktestSettings, FillPolicy, buy_hold, exposure_stats
from strata.config import RiskSettings, WatchlistEntry
from strata.data.base import DailyBar
from strata.data.repair import repair_split_jumps
from strata.strategy.base import Signal, SignalAction, Strategy
from strata.strategy.ml_probability import MLProbabilityStrategy

TOPIX30 = [
    "1605",
    "3382",
    "4004",
    "4502",
    "5401",
    "6146",
    "6301",
    "6501",
    "6702",
    "6758",
    "6857",
    "6902",
    "6963",
    "6981",
    "7203",
    "7267",
    "7269",
    "7751",
    "7951",
    "8001",
    "8002",
    "8035",
    "8058",
    "8306",
    "8604",
    "8766",
    "9101",
    "9201",
    "9432",
    "9433",
]
_EPS = 1e-9


class RandomControlStrategy(Strategy):
    """選択スキル分離用のランダム対照（発注の「型」はMLと同じ・方向だけ乱数）.

    各営業日、銘柄ごとに確率 p_entry で BUY、確率 p_exit で SELL（状態遷移で
    重複発注を抑える）。種を固定すれば再現可能。売買の「うまさ」はゼロなので、
    これと ML を同一基準（return / エクスポージャー）で比べれば、ML の銘柄
    選択がランダムを上乗せしているかが見える。
    """

    name = "random_control"

    def __init__(self, *, seed: int, p_entry: float = 0.05, p_exit: float = 0.12) -> None:
        self._rng = random.Random(seed)
        self._p_entry = p_entry
        self._p_exit = p_exit
        self._in: dict[str, bool] = {}

    def generate_signals(self, bars: Mapping[str, Sequence[DailyBar]]) -> list[Signal]:
        out: list[Signal] = []
        for symbol in bars:
            r = self._rng.random()
            if self._in.get(symbol):
                if r < self._p_exit:
                    self._in[symbol] = False
                    out.append(
                        Signal(symbol=symbol, action=SignalAction.SELL, reason="random exit")
                    )
            elif r < self._p_entry:
                self._in[symbol] = True
                out.append(Signal(symbol=symbol, action=SignalAction.BUY, reason="random entry"))
        return out


def load_bars(cache: Path, symbols: list[str]) -> dict[str, list[DailyBar]]:
    raw: dict[str, list[dict]] = json.loads(cache.read_text(encoding="utf-8"))
    out: dict[str, list[DailyBar]] = {}
    for symbol in symbols:
        if symbol not in raw:
            continue
        series = [DailyBar(symbol=symbol, source="yfinance", **rec) for rec in raw[symbol]]
        out[symbol] = repair_split_jumps(series)
    return out


def slice_bars(bars: dict[str, list[DailyBar]], start: str, end: str) -> dict[str, list[DailyBar]]:
    out = {s: [b for b in v if start <= b.date < end] for s, v in bars.items()}
    return {s: v for s, v in out.items() if len(v) >= 40}


def _risk() -> RiskSettings:
    return RiskSettings(max_notional_per_order=5_000_000, max_qty_per_order=2_000)


def _runner(strategy: Strategy, bars: dict[str, list[DailyBar]], cash: float) -> dict[str, object]:
    universe = [WatchlistEntry(symbol=s, trading_unit=100, stop_width=20) for s in bars]
    result = BacktestRunner(
        strategy=strategy,
        universe=universe,
        bars=bars,
        risk_settings=_risk(),
        settings=BacktestSettings(initial_cash=cash, fill=FillPolicy.NEXT_OPEN),
    ).run()
    m = dict(result.metrics)
    m["_open_positions"] = float(len(result.open_trades))
    m["_signals"] = float(len(result.signals))
    m["_round_trips"] = float(len(result.trades))
    wins = sum(1 for t in result.trades if t.pnl > 0)
    m["_win_rate"] = wins / len(result.trades) if result.trades else 0.0
    m["_expected_value"] = (
        sum(t.pnl for t in result.trades) / len(result.trades) if result.trades else 0.0
    )
    m["_curve"] = result.equity_curve
    m["_fills"] = result.fills
    return m


def eval_metrics(m: dict[str, object], eval_start: str) -> dict[str, float]:
    """走査済み成績を **評価期間のみ** で切り直して指標を返す（学習期間で薄めない）."""
    curve = [p for p in m["_curve"] if p.date >= eval_start] or list(m["_curve"])  # type: ignore[index]
    if not curve:
        return {}
    base = curve[0].equity
    ex = exposure_stats(curve)
    realized = float(m.get("realized_pnl", 0.0))
    final = float(m.get("final_equity", 0.0))
    fills = [f for f in m["_fills"] if f.date >= eval_start]  # type: ignore[index]
    traded_notional = sum(f.qty * f.price for f in fills)
    return {
        "total_return": final / base - 1.0 if base > 0 else 0.0,
        "realized_pnl": realized,
        "unrealized_pnl": final - base - realized,
        "end_market_value": curve[-1].market_value,
        "time_in_market": ex.time_in_market,
        "avg_exposure": ex.avg_exposure,
        "max_exposure": ex.max_exposure,
        "ret_per_exposure": (final / base - 1.0) / ex.avg_exposure
        if ex.avg_exposure > _EPS
        else 0.0,
        "notional_over_base": traded_notional / base if base > 0 else 0.0,
        "signals": float(m["_signals"]),  # type: ignore[index]
        "round_trips": float(m["_round_trips"]),  # type: ignore[index]
        "win_rate": float(m["_win_rate"]),  # type: ignore[index]
        "expected_value": float(m["_expected_value"]),  # type: ignore[index]
        "open_positions": float(m["_open_positions"]),  # type: ignore[index]
    }


def run_window(
    strategy: Strategy, bars: dict[str, list[DailyBar]], cash: float, eval_start: str
) -> dict[str, float]:
    return eval_metrics(_runner(strategy, bars, cash), eval_start)


def run_random(bars: dict[str, list[DailyBar]], cash: float, seed: int) -> dict[str, float]:
    """ランダム対照は評価期間だけで走らせる（学習期間は不要）."""
    m = _runner(RandomControlStrategy(seed=seed), bars, cash)
    return eval_metrics(m, bars[next(iter(bars))][0].date)


def bh(bars: dict[str, list[DailyBar]], start: str, end: str, cash: float) -> dict[str, float]:
    res = buy_hold(slice_bars(bars, start, end), cash)
    ex = exposure_stats(res.curve)
    return {
        "total_return": res.total_return,
        "avg_exposure": ex.avg_exposure,
        "ret_per_exposure": res.total_return / ex.avg_exposure if ex.avg_exposure > _EPS else 0.0,
    }


def calibration_rows(
    strategy: MLProbabilityStrategy,
    bars: dict[str, list[DailyBar]],
    start: str,
    end: str,
    band: float,
) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
    """評価期間の予測確率と実現ラベル (p, y) を up/down で返す（事後解析・未来参照は実現値のみ）."""
    up: list[tuple[float, int]] = []
    down: list[tuple[float, int]] = []
    for series in bars.values():
        ordered = sorted(series, key=lambda b: b.date)
        dates = [b.date for b in ordered]
        pos = {d: i for i, d in enumerate(dates)}
        closes = [b.close for b in ordered]
        for d, p_up, p_down in strategy.probs(ordered):
            if not (start <= d < end):
                continue
            i = pos[d]
            if i + 1 >= len(ordered):
                continue
            base, nxt = closes[i], closes[i + 1]
            up.append((p_up, 1 if nxt >= base * (1.0 + band) else 0))
            down.append((p_down, 1 if nxt <= base * (1.0 - band) else 0))
    return up, down


def calibration_table(pairs: list[tuple[float, int]], label: str) -> list[str]:
    """予測確率を帯に分けて件数・平均予測・実現発生率を出す（補正の当否を見る）."""
    edges = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    lines = [
        f"**{label}**（n={len(pairs)}）",
        "",
        "| 予測確率帯 | n | 平均予測 | 実現発生率 |",
        "|---|---|---|---|",
    ]
    for lo, hi in itertools.pairwise(edges):
        bucket = [(p, y) for p, y in pairs if lo <= p < hi]
        if not bucket:
            continue
        n = len(bucket)
        mean_p = sum(p for p, _ in bucket) / n
        hit = sum(y for _, y in bucket) / n
        lines.append(f"| {lo:.1f}〜{hi:.1f} | {n} | {mean_p * 100:.1f}% | {hit * 100:.1f}% |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default="var/reports/p9_bars.json")
    parser.add_argument("--outdir", default="var/reports")
    parser.add_argument("--eval-years", default="2019,2021,2023,2025")
    parser.add_argument("--train-years", type=int, default=4)
    parser.add_argument("--thresholds", default="0.70,0.60,0.55", help="カンマ区切りの発注確率閾値")
    parser.add_argument("--band", type=float, default=0.005)
    parser.add_argument(
        "--lag",
        type=int,
        default=1,
        help="ラベルの執行整合: 1=t+1→t+2（既定・実行可）/ 0=t→t+1（非実行系の対照）",
    )
    parser.add_argument("--cash", type=float, default=10_000_000.0)
    parser.add_argument("--costs", default="0,0.05,0.10", help="片道コスト率（%）カンマ区切り")
    parser.add_argument("--random-seeds", type=int, default=40, help="ランダム対照の試行回数")
    args = parser.parse_args()

    cache = Path(args.cache)
    eval_years = [int(y) for y in args.eval_years.split(",")]
    thresholds = [float(t) for t in args.thresholds.split(",")]
    ctrl_th = min(thresholds)

    all_bars = load_bars(cache, TOPIX30)
    trade_bars = dict(all_bars)

    def period(year: int) -> tuple[str, str, str]:
        return f"{year - args.train_years}-01-01", f"{year}-01-01", f"{year + 1}-01-01"

    lines: list[str] = [
        "# ML騰落確率戦略 walk-forward（クリーンルーム再実装）",
        "",
        "> 参照: hajime-f/stocktrading (CC BY-NC 4.0) の投資判断の考え方。",
        "> TensorFlow BiLSTM は使わず、scikit-learn MLP で独立再実装。**非営利の実験専用**。",
        "> 学習は評価年より前のみ（アウト・オブ・サンプル）。**成績は評価期間のみ**で集計。",
        "",
        f"- 条件: {len(trade_bars)}銘柄 / 資金 {args.cash:,.0f} 円 / 学習 {args.train_years}年 / "
        f"評価 {eval_years} / band ±{args.band * 100:.2f}% / 閾値 {thresholds} / "
        f"ラベル lag={args.lag}（{'t+1→t+2・執行整合' if args.lag == 1 else 't→t+1・非実行系'}）/ "
        f"C=0（SOR無料）",
        f"- データ: {cache}（2014-06〜2026-10）/ 実行日 {date.today().isoformat()}",
        "",
        "> 指標の注記: **投資時間=在市場日割**（1株でも保有した日）。**平均Eクスポ=資金投入率**",
        "> の期間平均。この2つは別物。return/exp=リターン÷平均エクスポージャー（投資量で正規化）。",
    ]
    print(lines[0])

    # --- 閾値別（監査列つき） ---
    for th in thresholds:
        print(f"\n##### 確率閾値 {th:.2f} #####")
        lines += ["", f"## A. 確率閾値 {th:.2f}（評価期間のみ・監査列つき）", ""]
        lines += [
            "| 年 | ML% | 実現% | 含み% | 投資時間 | 平均Eクスポ | "
            "期末建玉 | 往復 | E/回 | B&H% | vsB&H |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        win_bh = 0
        excess_sum = 0.0
        for year in eval_years:
            ts, te, ee = period(year)
            window = slice_bars(trade_bars, ts, ee)
            if not window:
                continue
            strat = MLProbabilityStrategy(
                train_start=ts, train_end=te, band=args.band, threshold=th, label_lag=args.lag
            )
            m = run_window(strat, window, args.cash, te)
            bench = bh(trade_bars, te, ee, args.cash)["total_return"]
            ret = m["total_return"]
            ex_bh = ret - bench
            win_bh += int(ex_bh > 0)
            excess_sum += ex_bh
            print(
                f"  {year}: {ret * 100:>7.2f}%  実現 {m['realized_pnl']:>11,.0f} "
                f"含み {m['unrealized_pnl']:>11,.0f}  投資時間 {m['time_in_market'] * 100:>5.1f}% "
                f"平均Eクスポ {m['avg_exposure'] * 100:>5.1f}%  期末建玉 {m['open_positions']:.0f} "
                f"vsB&H {ex_bh * 100:+7.2f}pt"
            )
            lines.append(
                f"| {year} | {ret * 100:+.2f}% | {m['realized_pnl']:,.0f} | "
                f"{m['unrealized_pnl']:,.0f} | {m['time_in_market'] * 100:.0f}% | "
                f"{m['avg_exposure'] * 100:.1f}% | {m['open_positions']:.0f} | "
                f"{m['round_trips']:.0f} | {m['expected_value']:,.0f} | {bench * 100:+.2f}% | "
                f"{ex_bh * 100:+.2f}pt |"
            )
        avg = excess_sum / max(len(eval_years), 1)
        lines.append(
            f"- まとめ: B&H勝ち {win_bh}/{len(eval_years)}（年平均超過 {avg * 100:+.2f}pt）"
        )
        print(f"  まとめ: B&H勝ち {win_bh}/{len(eval_years)}（年平均超過 {avg * 100:+.2f}pt）")

    # --- 対照・校正・コストは同一の学習（閾値 ctrl_th）を1年1回だけ走らせて共有 ---
    ctrl: dict[int, dict[str, float]] = {}
    ctrl_strat: dict[int, MLProbabilityStrategy] = {}
    rand: dict[int, dict[str, float]] = {}
    rand_dist: dict[int, list[float]] = {}
    bench: dict[int, dict[str, float]] = {}
    for year in eval_years:
        ts, te, ee = period(year)
        window = slice_bars(trade_bars, ts, ee)
        eval_only = slice_bars(trade_bars, te, ee)
        if not window or not eval_only:
            continue
        strat = MLProbabilityStrategy(
            train_start=ts, train_end=te, band=args.band, threshold=ctrl_th, label_lag=args.lag
        )
        ctrl[year] = run_window(strat, window, args.cash, te)
        ctrl_strat[year] = strat
        rr = [run_random(eval_only, args.cash, seed=1000 + s) for s in range(args.random_seeds)]
        dist = sorted(r["ret_per_exposure"] for r in rr)
        rand_dist[year] = dist
        rand[year] = {
            "rpe": sum(dist) / len(dist),
            "p50": dist[len(dist) // 2],
            "exp": sum(r["avg_exposure"] for r in rr) / len(rr),
        }
        bench[year] = bh(trade_bars, te, ee, args.cash)

    # --- 選択スキル分離（ML をランダム対照の分布の中の位置で評価） ---
    lines += [
        "",
        "---",
        "",
        f"## B. 選択スキルの分離（閾値 {ctrl_th:.2f}・return/exp ＝投資量で正規化した成果）",
        "",
        "投資時間・資金投入率が違う主体を裸のリターンで比べるのは不公平。",
        "そこで **リターン ÷ 平均エクスポージャー**（市場に置いた金1単位あたりの成果）で比べる。",
        f"ランダム対照を **{args.random_seeds} 通り**回し、ML がその分布の何分位にいるかで",
        "**銘柄選択の情報量**を見る（中央付近なら差は投資量だけ＝スキル無し）。",
        "",
        "| 年 | ML rpe | ランダム中央値 | MLの分位 | ML 平均Eクスポ | ランダムEクスポ | B&H rpe |",
        "|---|---|---|---|---|---|---|",
    ]
    ml_minus_rand = 0
    for year in ctrl:
        ml, r, b = ctrl[year], rand[year], bench[year]
        dist = rand_dist[year]
        pct = sum(1 for v in dist if v <= ml["ret_per_exposure"]) / len(dist)
        ml_minus_rand += int(pct > 0.5)
        print(
            f"  {year}: ML {ml['ret_per_exposure'] * 100:>7.1f}%  "
            f"ランダム中央 {r['p50'] * 100:>7.1f}% 分位 {pct * 100:>5.1f}%  "
            f"(ML Eクスポ {ml['avg_exposure'] * 100:.1f}% / "
            f"ランダム {r['exp'] * 100:.1f}% / B&H rpe {b['ret_per_exposure'] * 100:.1f}%)"
        )
        lines.append(
            f"| {year} | {ml['ret_per_exposure'] * 100:+.1f}% | {r['p50'] * 100:+.1f}% | "
            f"{pct * 100:.0f}% | {ml['avg_exposure'] * 100:.1f}% | {r['exp'] * 100:.1f}% | "
            f"{b['ret_per_exposure'] * 100:+.1f}% |"
        )
    lines.append(
        f"- ML がランダム中央値を上回った年（分位>50%）: **{ml_minus_rand}/{len(eval_years)}**"
        "（分位が中央付近なら選択スキルの証拠は無い）",
    )

    # --- 確率校正（評価期間・事後解析・ctrl の学習済みモデルを再利用） ---
    lines += [
        "",
        "---",
        "",
        f"## C. 予測確率の校正（閾値 {ctrl_th:.2f}・評価期間・予測は当日まで／実現のみ未来参照）",
        "",
        "MLP の 0.7 は「本当に70%発生」ではない。予測確率帯ごとの実現発生率を見て、",
        "閾値の絶対値が妥当か（慎重すぎ／過信）を判断する。",
        "",
    ]
    for year, strat in ctrl_strat.items():
        te, ee = f"{year}-01-01", f"{year + 1}-01-01"
        up, down = calibration_rows(strat, trade_bars, te, ee, args.band)
        lines += ["", f"### 評価 {year}", ""]
        lines += calibration_table(up, "上げる確率 (up)")
        lines.append("")
        lines += calibration_table(down, "下げる確率 (down)")
        print(f"  {year}: 校正サンプル up={len(up)} down={len(down)}")

    # --- D. コスト感度（片道コスト率を売買名簿額に課す。ML のみ実質的に効く） ---
    costs = [float(c) for c in args.costs.split(",")]
    lines += [
        "",
        "---",
        "",
        f"## D. コスト感度（閾値 {ctrl_th:.2f}・片道コスト率 × 売買名簿額）",
        "",
        "SOR手数料は無料だが**スリッページ／スプレッド**は無料ではない。片道率を",
        "売買名簿額（約定 qty×price、両脚）に課して評価期間リターンを引き直す。",
        "等額B&Hは買い1回で売買名簿額≈資金1回分のため実質無影響→**コストはML側だけ悪化**させる。",
        "",
        "| 片道コスト | " + " | ".join(str(y) for y in eval_years) + " | 平均 | B&H平均 |",
        "|---|" + "---|" * (len(eval_years) + 2),
    ]
    for rate in costs:
        cells: list[str] = []
        for y in eval_years:
            if y not in ctrl:
                cells.append("-")
                continue
            net = ctrl[y]["total_return"] - (rate / 100.0) * ctrl[y]["notional_over_base"]
            cells.append(f"{net * 100:+.2f}%")
        vals = [float(c.rstrip("%")) for c in cells if c != "-"]
        bh_vals = [bench[y]["total_return"] for y in ctrl]
        avg_net = sum(vals) / len(vals) if vals else 0.0
        bh_avg = sum(bh_vals) / len(bh_vals) if bh_vals else 0.0
        if rate == costs[0]:
            print(
                f"  cost {rate:.2f}%: "
                + " ".join(f"{y}={c}" for y, c in zip(eval_years, cells, strict=True))
            )
        lines.append(
            f"| {rate:.2f}% | " + " | ".join(cells) + f" | {avg_net:+.2f}% | {bh_avg * 100:+.2f}% |"
        )

    # --- E. ラベルhorizon感度（t→t+1 予測の約定1日遅れが結論を動するか） ---
    lines += [
        "",
        "---",
        "",
        f"## E. ラベルhorizon感度（閾値 {ctrl_th:.2f}・lag=0 t→t+1 vs lag=1 t+1→t+2）",
        "",
        "モデルは「シグナル日から先 ±band の騰落」を予測するが、約定は翌営業日終値",
        "（NEXT_OPEN近似）。`lag=0` は予測した変動が約定前に完了する**非実行系**、",
        "`lag=1` はエントリー後の変動を見る**実行系**。優位性が horizon に耐えるか確認する。",
        "",
        "| 年 | lag0 rpe | lag0 分位 | lag1 rpe | lag1 分位 | ランダム 中央値 |",
        "|---|---|---|---|---|---|",
    ]
    for year in ctrl:
        ts, te, ee = period(year)
        window = slice_bars(trade_bars, ts, ee)
        dist = rand_dist[year]
        med = rand[year]["p50"]
        cells = []
        for lag in (0, 1):
            if lag == args.lag:
                rpe = ctrl[year]["ret_per_exposure"]
            else:
                st = MLProbabilityStrategy(
                    train_start=ts, train_end=te, band=args.band, threshold=ctrl_th, label_lag=lag
                )
                rpe = run_window(st, window, args.cash, te)["ret_per_exposure"]
            pct = sum(1 for v in dist if v <= rpe) / len(dist)
            cells += [f"{rpe * 100:+.1f}%", f"{pct * 100:.0f}%"]
        print(f"  {year}: lag0 {cells[0]}（{cells[1]}） lag1 {cells[2]}（{cells[3]}）")
        lines.append(f"| {year} | " + " | ".join(cells) + f" | {med * 100:+.1f}% |")

    lines += [
        "",
        "---",
        "",
        "## 解釈（判定は docs/FINDINGS.md F-006）",
        "",
        "- 訂正: 投資時間=在市場日割で**閾値0.55/0.60 では 80〜99%・平均Eクスポ（資金投入率）も",
        "  60〜90%**と実はフル投資に近い。旧レポートの「投資時間19%＝市場に居ない」は走査窓",
        "  （学習4年込みの5年）で割った**計測誤り**で、評価期間だけで測り直して訂正した",
        "- 敗因は投資量ではない。**Headline return の大半が期末建玉の含み益**で、実現損益は",
        "  窓により符号が異なり小幅（A表の 実現% / 含み% を参照）。ただし含み益が市場ベータだけか",
        "  選択スキルを含むかは本検証では未分離（TOPIX比の帰属は別途）",
        "- 選択スキルの**証拠は得られなかった**: return/exp（投資量で正規化）で、MLはランダム対照",
        "  の中央値に対し分位が中央〜下位にとどまる。断定は避け「この実装・条件では選択スキルを",
        "  示す証拠なし」とする（lag=1 の実行系でも同じ）",
        "- 校正が崩れている: 中高確率帯の実現発生率は予測を大きく下回り**著しい過信**。閾値 0.7 は",
        "  「70%の確信」ではなく、高発動帯は数件で運が支配的（日次予測は自己相関で実効サンプルは",
        "  件数より少ない。再調整するなら未使用窓で再評価）",
        "- horizon: 既定 lag=1（エントリー後の変動）で測り、約定1日遅れを織り込み済み。lag=0 と",
        "  比較して E で結論が耐えるか確認する",
        "- コスト感度: 売買名簿額が資金の年複数月分にのぼり、片道 0.05% でも年平均が",
        "  明確に下がる。等額B&Hは買い1回でほぼ無影響なので、**コストをかけるほど差は開く**",
        "- 判定: **採用不可（P5不開設）**。ただしこれは「MLP・ロングオンリー・このサイジング」の",
        "  結論であり、BiLSTM/L/S そのものの否証でも優位性の不在証明でもない。4窓は検定力が小さい",
    ]
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report = outdir / "ml.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nレポート: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
