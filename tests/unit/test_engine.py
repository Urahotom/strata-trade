"""1営業日エンジン（DailyEngine）のテスト.

TESTING §3.4: 当日バー欠損で取引中断、ロスカット執行、日次サマリ。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from strata.api.models import PositionItem
from strata.backtest.broker import FillPolicy, PaperBroker
from strata.config import DataSettings, RiskSettings, WatchlistEntry
from strata.data.base import DailyBar, MarketDataProvider
from strata.engine.day import DailyEngine, DayPhase
from strata.notify.base import InMemoryNotifier, Severity
from strata.order.manager import OrderManager
from strata.order.repository import IntentRepository
from strata.risk.checks import RiskEngine
from strata.risk.killswitch import KillSwitch
from strata.store import init_db
from strata.strategy.base import Signal, SignalAction, Strategy

TRADE_DATE = date(2026, 10, 23)


class StubProvider(MarketDataProvider):
    name = "manual"

    def __init__(self, bars: list[DailyBar]) -> None:
        self._bars = bars

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        return [b for b in self._bars if start <= date.fromisoformat(b.date) <= end]


class BuyOnce(Strategy):
    name = "buy_once"

    def __init__(self) -> None:
        self._fired = False

    def generate_signals(self, bars) -> list[Signal]:  # type: ignore[no-untyped-def]
        if self._fired or not bars:
            return []
        self._fired = True
        return [Signal(symbol="8306", action=SignalAction.BUY, reason="test")]


def make_bars(n: int, close: float = 100.0, *, end: date = TRADE_DATE) -> list[DailyBar]:
    start = end - timedelta(days=n - 1)
    return [
        DailyBar(
            symbol="8306",
            date=(start + timedelta(days=i)).isoformat(),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=100,
            source="manual",
        )
        for i in range(n)
    ]


@pytest.fixture()
def repo(tmp_path) -> IntentRepository:  # type: ignore[no-untyped-def]
    conn = init_db(tmp_path / "engine.db")
    yield IntentRepository(conn)
    conn.close()


def make_engine(
    repo: IntentRepository,
    *,
    bars: list[DailyBar],
    strategy: Strategy | None = None,
    data_settings: DataSettings | None = None,
    kill: KillSwitch | None = None,
    price_fn=lambda s: None,  # type: ignore[no-untyped-def]
    positions_fn=lambda: (),  # type: ignore[no-untyped-def]
    profit_fn=None,  # type: ignore[no-untyped-def]
) -> tuple[DailyEngine, PaperBroker, InMemoryNotifier]:
    broker = PaperBroker(initial_cash=1_000_000, fill=FillPolicy.CLOSE)
    broker.set_market_day(TRADE_DATE.isoformat(), {"8306": 100.0})
    notifier = InMemoryNotifier()
    manager = OrderManager(repo, broker, notifier, kill_switch=kill)  # type: ignore[arg-type]
    engine = DailyEngine(
        provider=StubProvider(bars),
        strategy=strategy or BuyOnce(),
        universe=[WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)],
        manager=manager,  # type: ignore[arg-type]
        risk=RiskEngine(RiskSettings()),
        repo=repo,
        notifier=notifier,
        settings=data_settings or DataSettings(),
        kill_switch=kill,
        price_fn=price_fn,
        positions_fn=positions_fn,
        profit_fn=profit_fn,
    )
    return engine, broker, notifier


def pos(symbol: str, qty: int, price: float = 100.0) -> PositionItem:
    return PositionItem(Symbol=symbol, Exchange=9, Side="2", HoldQty=qty, Price=price)


class TestPrepare:
    def test_正常データでPREPARED(self, repo) -> None:
        engine, _broker, _ = make_engine(repo, bars=make_bars(5))
        assert engine.prepare(TRADE_DATE) is True
        assert engine.phase is DayPhase.PREPARED

    def test_当日バー欠損で中断(self, repo) -> None:
        bars = make_bars(5, end=TRADE_DATE - timedelta(days=3))
        engine, _, notifier = make_engine(repo, bars=bars)
        assert engine.prepare(TRADE_DATE) is False
        assert engine.phase is DayPhase.HALTED
        assert notifier.subjects(Severity.ALERT)

    def test_中断設定オフなら警告のみで継続(self, repo) -> None:
        bars = make_bars(5, end=TRADE_DATE - timedelta(days=3))
        engine, _, notifier = make_engine(
            repo, bars=bars, data_settings=DataSettings(halt_when_today_bar_missing=False)
        )
        assert engine.prepare(TRADE_DATE) is True
        assert engine.phase is DayPhase.PREPARED
        assert notifier.subjects(Severity.ALERT)

    def test_全バー異常で中断(self, repo) -> None:
        bad = [
            DailyBar(
                symbol="8306",
                date=TRADE_DATE.isoformat(),
                open=1,
                high=1,
                low=9,
                close=1,
                source="manual",
            )
        ]
        engine, _, _ = make_engine(repo, bars=bad)
        assert engine.prepare(TRADE_DATE) is False
        assert engine.phase is DayPhase.HALTED


class TestAct:
    def test_PREPARED以外では実行不可(self, repo) -> None:
        engine, _, _ = make_engine(repo, bars=make_bars(5))
        with pytest.raises(RuntimeError):
            engine.act(TRADE_DATE)

    def test_BUYシグナルで発注されロスカットラインを記録(self, repo) -> None:
        engine, broker, _ = make_engine(repo, bars=make_bars(5))
        engine.prepare(TRADE_DATE)
        signals = engine.act(TRADE_DATE)
        assert len(signals) == 1
        assert engine.phase is DayPhase.MONITORING
        # 意図台帳に BUY が残り、建玉が発生（CLOSE約定）
        assert broker.held_qty("8306") > 0
        intents = repo.list_intents_for_date(TRADE_DATE.isoformat())
        assert len(intents) == 1
        assert str(intents[0].side) == "BUY"
        # stop = ref(100) - width(10)
        assert engine.stops["8306"] == pytest.approx(90.0)


class TestMonitor:
    def test_価格がロスカットライン割りで成行売り(self, repo) -> None:
        engine, broker, notifier = make_engine(
            repo,
            bars=make_bars(5),
            positions_fn=lambda: [pos("8306", 100)],
            price_fn=lambda _s: 85.0,
        )
        engine.prepare(TRADE_DATE)
        engine.act(TRADE_DATE)  # 100でBUY → stop=90
        held_after_buy = broker.held_qty("8306")
        triggered = engine.monitor()
        assert triggered == ["8306"]
        assert broker.held_qty("8306") < held_after_buy
        assert notifier.subjects(Severity.ALERT)

    def test_日次損失でキルスイッチ(self, repo) -> None:
        kill = KillSwitch(max_daily_loss=50_000)
        engine, _, notifier = make_engine(
            repo,
            bars=make_bars(5),
            kill=kill,
            profit_fn=lambda: -60_000.0,
            positions_fn=lambda: [pos("8306", 100)],
        )
        engine.prepare(TRADE_DATE)
        engine.act(TRADE_DATE)
        engine.monitor()
        assert kill.active
        assert notifier.subjects(Severity.CRITICAL)

    def test_MONITORING以外では監視しない(self, repo) -> None:
        engine, _, _ = make_engine(repo, bars=make_bars(5), price_fn=lambda _s: 1.0)
        assert engine.monitor() == []


class TestFinish:
    def test_日次サマリと建玉スナップショット(self, repo) -> None:
        engine, _, notifier = make_engine(
            repo, bars=make_bars(5), positions_fn=lambda: [pos("8306", 100)]
        )
        engine.prepare(TRADE_DATE)
        engine.act(TRADE_DATE)
        engine.finish(TRADE_DATE)
        assert engine.phase is DayPhase.FINISHED
        assert notifier.subjects(Severity.INFO)


class TestRunDay:
    def test_通し実行でFINISHED(self, repo) -> None:
        engine, broker, _ = make_engine(repo, bars=make_bars(5))
        phase = engine.run_day(TRADE_DATE)
        assert phase is DayPhase.FINISHED
        assert broker.held_qty("8306") > 0

    def test_中断なら発注なしでFINISHED(self, repo) -> None:
        bars = make_bars(5, end=TRADE_DATE - timedelta(days=3))
        engine, broker, _ = make_engine(repo, bars=bars)
        phase = engine.run_day(TRADE_DATE)
        assert phase is DayPhase.FINISHED
        assert broker.held_qty("8306") == 0
        assert repo.list_intents_for_date(TRADE_DATE.isoformat()) == []
