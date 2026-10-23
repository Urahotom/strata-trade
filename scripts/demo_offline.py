"""P3 動作確認デモ（ネットワーク・実口座不要・完全オフライン）.

A) MAクロス戦略のバックテスト: 戦略→リスク→意図台帳→約定→成績
B) 1営業日エンジン: 当日バー欠損で自動中断する安全装置

実行: `uv run python scripts/demo_offline.py`（docs/VERIFY.md §P3）
"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from strata.backtest import BacktestRunner, BacktestSettings, FillPolicy
from strata.backtest.broker import PaperBroker
from strata.config import DataSettings, RiskSettings, WatchlistEntry
from strata.data.base import DailyBar, MarketDataProvider
from strata.engine import DailyEngine
from strata.notify.base import InMemoryNotifier, Severity
from strata.order.manager import OrderManager
from strata.order.repository import IntentRepository
from strata.risk.checks import RiskEngine
from strata.store import init_db
from strata.strategy import MovingAverageCross


def mk(symbol: str, prices: list[float], start: date = date(2024, 1, 2)) -> list[DailyBar]:
    out: list[DailyBar] = []
    d = start
    for p in prices:
        while d.weekday() >= 5:  # 週末飛ばし
            d += timedelta(days=1)
        out.append(
            DailyBar(
                symbol=symbol,
                date=d.isoformat(),
                open=p,
                high=p * 1.01,
                low=p * 0.99,
                close=p,
                volume=1_000_000,
                source="manual",
            )
        )
        d += timedelta(days=1)
    return out


def demo_backtest() -> None:
    print("=" * 60)
    print("A) MAクロス戦略バックテスト")
    print("=" * 60)
    # 下落 → 急反発(ゴールデンクロス) → 高値圏で失速(デッドクロス)
    prices = (
        [100 - 0.5 * i for i in range(20)]
        + [90 + 1.5 * i for i in range(20)]
        + [120 - 0.8 * i for i in range(15)]
    )
    bars = mk("8306", prices)
    print(f"バー数: {len(bars)}  期間: {bars[0].date}〜{bars[-1].date}")

    runner = BacktestRunner(
        strategy=MovingAverageCross(fast=5, slow=25),
        universe=[WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)],
        bars={"8306": bars},
        risk_settings=RiskSettings(),
        settings=BacktestSettings(initial_cash=1_000_000, fill=FillPolicy.CLOSE),
    )
    result = runner.run()
    buys = sum(1 for s in result.signals if str(s.action) == "BUY")
    sells = sum(1 for s in result.signals if str(s.action) == "SELL")
    print(f"シグナル: BUY {buys} / SELL {sells}  見送り: {result.rejected_signals}")
    m = result.metrics
    print("--- 成績 ---")
    print(f"初期資金      : {m['initial_cash']:>14,.0f}")
    print(f"最終資産      : {m.get('final_equity', 0):>14,.0f}")
    print(f"総リターン    : {m.get('total_return', 0) * 100:>13,.2f} %")
    print(f"最大DD        : {m.get('max_drawdown', 0) * 100:>13,.2f} %")
    print(f"約定数        : {m['executions']:>14,.0f}")
    print(f"win率         : {m['win_rate'] * 100:>13,.1f} %")
    print("最終5日のエクイティ曲線:")
    for p in result.equity_curve[-5:]:
        print(f"  {p.date}: 資産 {p.equity:,.0f} (現金 {p.cash:,.0f} / 評価 {p.market_value:,.0f})")


class StubProvider(MarketDataProvider):
    name = "manual"

    def __init__(self, bars: list[DailyBar]) -> None:
        self._bars = bars

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        return self._bars


def demo_halt() -> None:
    print()
    print("=" * 60)
    print("B) 1営業日エンジン: 当日バー欠損で自動中断")
    print("=" * 60)
    today = date(2026, 10, 23)
    # 3営業日前までのデータしか無い → 当日バー欠損
    old = mk("8306", [100.0] * 5, start=today - timedelta(days=12))
    print(f"最新バー日付: {old[-1].date}  取引日: {today}  (=> 当日バー欠損)")

    tmp = Path(tempfile.mkdtemp()) / "demo.db"
    conn = init_db(tmp)
    repo = IntentRepository(conn)
    broker = PaperBroker(initial_cash=1_000_000, fill=FillPolicy.CLOSE)
    broker.set_market_day(today.isoformat(), {"8306": 100.0})
    notifier = InMemoryNotifier()
    manager = OrderManager(repo, broker, notifier)

    engine = DailyEngine(
        provider=StubProvider(old),
        strategy=MovingAverageCross(fast=2, slow=4),
        universe=[WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)],
        manager=manager,
        risk=RiskEngine(RiskSettings()),
        repo=repo,
        notifier=notifier,
        settings=DataSettings(halt_when_today_bar_missing=True),
    )
    phase = engine.run_day(today)
    print(f"phase: {phase.value}")
    print(f"発注意図の数: {len(repo.list_intents_for_date(today.isoformat()))} (0 が正しい)")
    print(f"発した警告: {notifier.subjects(Severity.ALERT)}")
    conn.close()


if __name__ == "__main__":
    demo_backtest()
    demo_halt()
