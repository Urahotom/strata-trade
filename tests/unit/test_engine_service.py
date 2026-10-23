"""P4 ペーパートレードサービス（service.py / __main__.py）のテスト.

TESTING §3.6 相当: スケジュール進行・認証失敗・キルスイッチ起動時の中止・
Ctrl+C 停止の終日処理・監視の一時失敗継続・設定検証。すべてオフライン。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from strata.api.models import TokenResult
from strata.api.results import ApiSuccess
from strata.config import (
    ApiSettings,
    EngineSettings,
    SmtpSettings,
    StorageSettings,
    WatchlistEntry,
)
from strata.config import Settings as AppSettings
from strata.engine.day import DayPhase
from strata.engine.service import PaperTrader, Runtime, build_runtime, make_strategy
from strata.errors import ConfigurationError
from strata.notify.base import InMemoryNotifier, LoggingNotifier, Severity
from strata.notify.mailer import make_notifier
from strata.order.manager import KillSwitchActiveError, StartupReport
from strata.risk.killswitch import KillSwitch
from strata.strategy.moving_average import MovingAverageCross

TRADE_DATE = date(2026, 10, 23)


class Clock:
    """手動進行する時計。sleep_fn を渡すと疑似的に時間軸を動かせます。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class StubClient:
    def __init__(self, auth_ok: bool = True) -> None:
        self._auth_ok = auth_ok
        self.auth_calls = 0

    def authenticate(self):  # type: ignore[no-untyped-def]
        self.auth_calls += 1
        if self._auth_ok:
            return ApiSuccess(data=TokenResult(ResultCode=0, Token="t"))
        return object()  # ApiSuccess 以外なら失敗扱い


class StubManager:
    def __init__(self, kill: KillSwitch | None = None, orphans: list[dict] | None = None) -> None:
        self.kill = kill
        self.orphans = orphans or []
        self.reconcile_calls: list[str] = []

    def startup_reconcile(self, trade_date):  # type: ignore[no-untyped-def]
        self.reconcile_calls.append(str(trade_date))
        if self.orphans and self.kill is not None:
            self.kill.activate("オーファン建玉を検知")
        return StartupReport(
            open_intents=[],
            orphan_positions=list(self.orphans),
            snapshot_captured_at="2026-10-23T00:00:00+00:00",
            positions_available=True,
        )


class StubEngine:
    def __init__(
        self,
        *,
        prepare_ok: bool = True,
        act_raises: Exception | None = None,
        monitor_raises: list[Exception] | None = None,
    ) -> None:
        self.phase = DayPhase.NEW
        self.halted_reason: str | None = None
        self._prepare_ok = prepare_ok
        self._act_raises = act_raises
        self._monitor_raises = list(monitor_raises or [])
        self.prepare_calls = 0
        self.act_calls = 0
        self.monitor_calls = 0
        self.finish_calls = 0

    def prepare(self, trade_date: date) -> bool:
        self.prepare_calls += 1
        if self._prepare_ok:
            self.phase = DayPhase.PREPARED
        else:
            self.phase = DayPhase.HALTED
            self.halted_reason = "テスト中断"
        return self._prepare_ok

    def act(self, trade_date: date) -> None:
        self.act_calls += 1
        if self._act_raises is not None:
            self.phase = DayPhase.HALTED
            raise self._act_raises
        self.phase = DayPhase.MONITORING

    def monitor(self) -> list[str]:
        self.monitor_calls += 1
        if self._monitor_raises:
            raise self._monitor_raises.pop(0)
        return []

    def finish(self, trade_date: date) -> None:
        self.finish_calls += 1
        self.phase = DayPhase.FINISHED


def make_settings(tmp_path: Path, **engine_kwargs) -> AppSettings:  # type: ignore[no-untyped-def]
    defaults: dict = {
        "act_time": "09:00",
        "close_time": "09:01",
        "monitor_interval_s": 30.0,
    }
    defaults.update(engine_kwargs)
    return AppSettings(
        api=ApiSettings(environment="test"),
        storage=StorageSettings(db_path=tmp_path / "service.db"),
        engine=EngineSettings(**defaults),
        watchlist=[WatchlistEntry(symbol="8306", trading_unit=100, stop_width=10)],
    )


def make_runtime(
    tmp_path: Path,
    *,
    client: StubClient | None = None,
    manager: StubManager | None = None,
    engine: StubEngine | None = None,
    kill: KillSwitch | None = None,
    **engine_kwargs,  # type: ignore[no-untyped-def]
) -> tuple[Runtime, Clock, InMemoryNotifier]:
    settings = make_settings(tmp_path, **engine_kwargs)
    notifier = InMemoryNotifier()
    kill = kill or KillSwitch(max_daily_loss=50_000)
    runtime = Runtime(
        settings=settings,
        conn=None,  # PaperTrader は DBにさわらない
        repo=None,
        client=client or StubClient(),  # type: ignore[arg-type]
        notifier=notifier,
        kill=kill,
        manager=manager or StubManager(kill=kill),  # type: ignore[arg-type]
        engine=engine or StubEngine(),  # type: ignore[arg-type]
    )
    clock = Clock(datetime(2026, 10, 23, 8, 59, 30))
    return runtime, clock, notifier


def make_trader(runtime: Runtime, clock: Clock, sleep_fn=None) -> PaperTrader:  # type: ignore[no-untyped-def]
    return PaperTrader(runtime, clock=clock, sleep_fn=sleep_fn or clock.advance)


class TestRunSchedule:
    def test_時刻どおりにprepare_act_監視_finish(self, tmp_path: Path) -> None:
        engine = StubEngine()
        runtime, clock, _ = make_runtime(tmp_path, engine=engine)
        trader = make_trader(runtime, clock)

        phase = trader.run(TRADE_DATE)

        assert phase is DayPhase.FINISHED
        assert engine.prepare_calls == 1 and engine.act_calls == 1
        # 09:00 と 09:00:30 に監視、09:01 で終了
        assert engine.monitor_calls == 2
        assert engine.finish_calls == 1
        # act は act_time 以降に実行されている
        assert clock() >= datetime(2026, 10, 23, 9, 0)

    def test_認証失敗なら発注せずにHALTED(self, tmp_path: Path) -> None:
        engine = StubEngine()
        client = StubClient(auth_ok=False)
        runtime, clock, notifier = make_runtime(tmp_path, client=client, engine=engine)
        trader = make_trader(runtime, clock)

        phase = trader.run(TRADE_DATE)

        assert phase is DayPhase.HALTED
        assert engine.prepare_calls == 0 and engine.finish_calls == 0
        assert "認証に失敗したため本日は運転しません" in notifier.subjects(Severity.CRITICAL)

    def test_オーファン建玉でbootstrap中止(self, tmp_path: Path) -> None:
        engine = StubEngine()
        kill = KillSwitch(max_daily_loss=50_000)
        manager = StubManager(kill=kill, orphans=[{"Symbol": "9999", "HoldQty": 100}])
        runtime, clock, _ = make_runtime(tmp_path, manager=manager, engine=engine, kill=kill)
        trader = make_trader(runtime, clock)

        phase = trader.run(TRADE_DATE)

        assert phase is DayPhase.HALTED
        assert engine.prepare_calls == 0
        assert kill.active
        assert manager.reconcile_calls == [str(TRADE_DATE)]

    def test_起動時リコンサイルは当日日付(self, tmp_path: Path) -> None:
        manager = StubManager()
        runtime, clock, _ = make_runtime(tmp_path, manager=manager)
        make_trader(runtime, clock).run(TRADE_DATE)
        assert manager.reconcile_calls == ["2026-10-23"]


class TestRunInterruption:
    def test_prepare中断ならactなしでfinish(self, tmp_path: Path) -> None:
        engine = StubEngine(prepare_ok=False)
        runtime, clock, _ = make_runtime(tmp_path, engine=engine)

        phase = make_trader(runtime, clock).run(TRADE_DATE)

        assert phase is DayPhase.FINISHED  # finish まで進む（中断理由は残る）
        assert engine.halted_reason == "テスト中断"
        assert engine.act_calls == 0 and engine.finish_calls == 1
        assert engine.monitor_calls == 0

    def test_actでキルスイッチはHALTEDでfinish(self, tmp_path: Path) -> None:
        engine = StubEngine(act_raises=KillSwitchActiveError("発注停止"))
        runtime, clock, _ = make_runtime(tmp_path, engine=engine)

        phase = make_trader(runtime, clock).run(TRADE_DATE)

        assert phase is DayPhase.FINISHED  # finish まで到達
        assert engine.monitor_calls == 0 and engine.finish_calls == 1

    def test_Ctrl_Cでact前に停止してもfinishする(self, tmp_path: Path) -> None:
        engine = StubEngine()
        runtime, clock, notifier = make_runtime(tmp_path, engine=engine)

        def stop_now(_seconds: float) -> None:
            raise KeyboardInterrupt

        phase = make_trader(runtime, clock, sleep_fn=stop_now).run(TRADE_DATE)

        assert phase is DayPhase.FINISHED
        assert engine.prepare_calls == 0 and engine.finish_calls == 1
        assert "終了シグナルを受け取り終日処理へ移行します" in notifier.subjects(Severity.ALERT)

    def test_監視の一時失敗は通知して続行(self, tmp_path: Path) -> None:
        engine = StubEngine(monitor_raises=[RuntimeError("通信断")])
        runtime, clock, notifier = make_runtime(tmp_path, engine=engine)

        phase = make_trader(runtime, clock).run(TRADE_DATE)

        assert phase is DayPhase.FINISHED
        assert engine.monitor_calls == 2  # 失敗→再試行で継続
        assert "監視の1周に失敗（再試行します）" in notifier.subjects(Severity.ALERT)


class TestBootstrapOnly:
    def test_bootstrap成功でレポート(self, tmp_path: Path) -> None:
        runtime, clock, _notifier = make_runtime(tmp_path)
        report = make_trader(runtime, clock).bootstrap()
        assert report is not None
        assert report.positions_available is True

    def test_bootstrapでキルスイッチ作動済みならNone(self, tmp_path: Path) -> None:
        kill = KillSwitch(max_daily_loss=50_000)
        kill.activate("手動")
        runtime, clock, _notifier = make_runtime(tmp_path, kill=kill)
        assert make_trader(runtime, clock).bootstrap() is None


class TestBuildRuntime:
    def test_プロバイダ無しでも組める_reconcile用途(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        runtime = build_runtime(settings, client=StubClient(), require_provider=False)  # type: ignore[arg-type]
        try:
            assert runtime.engine is not None
            # プロバイダ未設定の prepare は中断になる
            assert runtime.engine.prepare(TRADE_DATE) is False
            assert runtime.engine.halted_reason is not None
        finally:
            runtime.conn.close()

    def test_必須指定でキー未設定なら設定エラー(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with pytest.raises(ConfigurationError):
            build_runtime(settings, client=StubClient(), require_provider=True)  # type: ignore[arg-type]

    def test_戦略はMAクロス(self) -> None:
        strategy = make_strategy(EngineSettings(ma_fast=3, ma_slow=9))
        assert isinstance(strategy, MovingAverageCross)

    def test_戦略選択が4種そろう(self) -> None:
        from strata.strategy import (
            BollingerMeanReversion,
            DonchianBreakout,
            RSIMeanReversion,
        )

        assert isinstance(make_strategy(EngineSettings(strategy="rsi_reversion")), RSIMeanReversion)
        assert isinstance(
            make_strategy(EngineSettings(strategy="bollinger_reversion")),
            BollingerMeanReversion,
        )
        assert isinstance(
            make_strategy(EngineSettings(strategy="donchian_breakout")), DonchianBreakout
        )


class TestNotifierFactory:
    def test_smtp無効はログのみ(self) -> None:
        assert isinstance(make_notifier(SmtpSettings(enabled=False)), LoggingNotifier)

    def test_smtp有効はメール(self) -> None:
        notifier = make_notifier(SmtpSettings(enabled=True, to_addr="t@example.com"))
        assert type(notifier).__name__ == "MailNotifier"


class TestEngineSettings:
    def test_不正時刻形式(self) -> None:
        with pytest.raises(ValueError):
            EngineSettings(act_time="25:61")

    def test_終了は開始より後(self) -> None:
        with pytest.raises(ValueError):
            EngineSettings(act_time="15:00", close_time="09:00")

    def test_maは速い線が小さい(self) -> None:
        with pytest.raises(ValueError):
            EngineSettings(ma_fast=25, ma_slow=5)
