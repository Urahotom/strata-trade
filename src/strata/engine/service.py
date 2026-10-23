"""P4 ペーパートレード: 本番配線（依存の組み立て）と1営業日のスケジュール実行.

DESIGN §3.4 「1営業日 = 1プロセス」の配線役:
- `PaperTrader.bootstrap()`: 認証 → 起動時リコンサイル（オーファン建玉でキルスイッチ）
- act_time に prepare（データ検証）→ act（シグナル執行）
- close_time まで monitor_interval_s 間隔で約定同期・ロスカット監視
- finish(): 建玉スナップショット保存と日次サマリ通知

日次の再利用はOSのスケジューラ（タスクスケジューラ等）に委ねる。
常駐プロセスを持たないことが、日跨ぎの状態漏れを防ぐ設計の一部。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from typing import Final

from strata.api.client import KabuClient
from strata.api.models import PositionItem
from strata.api.results import ApiSuccess
from strata.config import EngineSettings, Settings
from strata.data import make_provider
from strata.data.base import MarketDataProvider
from strata.data.store import BarRepository
from strata.engine.day import DailyEngine, DayPhase
from strata.errors import ConfigurationError
from strata.notify.base import Notifier, Severity
from strata.notify.mailer import make_notifier
from strata.order.manager import KillSwitchActiveError, OrderManager, StartupReport
from strata.order.repository import IntentRepository
from strata.risk.checks import RiskEngine
from strata.risk.killswitch import KillSwitch
from strata.store.db import init_db
from strata.strategy.base import Strategy
from strata.strategy.moving_average import MovingAverageCross
from strata.strategy.technical import (
    BollingerMeanReversion,
    DonchianBreakout,
    RSIMeanReversion,
)

#: 待機中にCtrl+Cへ反応できるよう睡眠を分割する間隔（秒）
_WAIT_CHUNK_S: Final = 5.0


@dataclass
class Runtime:
    """エンジン1日分を構成する部品一式（テストでは部品ごと差し替える）."""

    settings: Settings
    conn: sqlite3.Connection
    repo: IntentRepository
    client: KabuClient
    notifier: Notifier
    kill: KillSwitch
    manager: OrderManager
    engine: DailyEngine


#: 設定名 -> 戦略コンストラクタ（パラメータ既定値は各戦略の設計上の標準値）
_STRATEGY_BUILDERS: dict[str, Callable[[], Strategy]] = {
    "ma_cross": lambda: MovingAverageCross(fast=5, slow=25),
    "rsi_reversion": RSIMeanReversion,
    "bollinger_reversion": BollingerMeanReversion,
    "donchian_breakout": DonchianBreakout,
}


def make_strategy(settings: EngineSettings) -> Strategy:
    """設定から戦略を生成する。パラメータ調整はバックテスト側で行う."""
    if settings.strategy == "ma_cross":
        return MovingAverageCross(fast=settings.ma_fast, slow=settings.ma_slow)
    builder = _STRATEGY_BUILDERS.get(settings.strategy)
    if builder is None:
        raise ConfigurationError(f"未対応の戦略です: {settings.strategy}")
    return builder()


def build_runtime(
    settings: Settings,
    *,
    client: KabuClient | None = None,
    provider: MarketDataProvider | None = None,
    require_provider: bool = False,
) -> Runtime:
    """設定から1営業日エンジンの部品を組み立てる（DBは初期化済み）.

    Args:
        client: 注入用クライアント（既定は設定から KabuClient）
        provider: 注入用データプロバイダ
        require_provider: True のときデータキー未設定で ConfigurationError。
            False なら None プロバイダ（prepare で取引中断になる）で組み立てる。
    """
    conn = init_db(settings.storage.db_path)
    notifier = make_notifier(settings.smtp)
    resolved_client = client or KabuClient(settings)
    kill = KillSwitch(max_daily_loss=settings.risk.max_daily_loss)
    risk = RiskEngine(settings.risk)
    repo = IntentRepository(conn)
    manager = OrderManager(repo, resolved_client, notifier, risk_engine=risk, kill_switch=kill)
    resolved_provider: MarketDataProvider | None = provider
    if resolved_provider is None:
        try:
            resolved_provider = make_provider(settings.data)
        except ConfigurationError:
            if require_provider:
                raise
            resolved_provider = None
    engine = DailyEngine(
        provider=resolved_provider,
        strategy=make_strategy(settings.engine),
        universe=settings.watchlist,
        manager=manager,
        risk=risk,
        repo=repo,
        notifier=notifier,
        settings=settings.data,
        bars_repo=BarRepository(conn),
        kill_switch=kill,
        price_fn=_price_fn(resolved_client),
        positions_fn=_positions_fn(resolved_client),
        profit_fn=_profit_fn(resolved_client),
    )
    return Runtime(
        settings=settings,
        conn=conn,
        repo=repo,
        client=resolved_client,
        notifier=notifier,
        kill=kill,
        manager=manager,
        engine=engine,
    )


class PaperTrader:
    """DailyEngine を1営業日ぶん時刻付きで回す（1日1プロセス前提）."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rt = runtime
        self._clock = clock or (lambda: datetime.now())
        self._sleep = sleep_fn
        self._stopping = False

    # ------------------------------------------------------------------ #

    def bootstrap(self) -> StartupReport | None:
        """認証と起動時リコンサイル。続行不可なら None（通知済み）."""
        rt = self._rt
        result = rt.client.authenticate()
        if not isinstance(result, ApiSuccess):
            rt.notifier.notify(
                Severity.CRITICAL, "認証に失敗したため本日は運転しません", str(result)[:300]
            )
            return None
        report = rt.manager.startup_reconcile(self._today())
        rt.notifier.notify(
            Severity.INFO,
            "起動時リコンサイル完了",
            f"未了意図 {len(report.open_intents)} 件 / オーファン建玉 "
            f"{len(report.orphan_positions)} 件 / 建玉照会"
            f"={'成功' if report.positions_available else '失敗'}",
        )
        if rt.kill.active:
            rt.notifier.notify(
                Severity.CRITICAL,
                "キルスイッチ作動中のため本日は運転しません",
                "; ".join(rt.kill.reasons),
            )
            return None
        return report

    def run(self, trade_date: date) -> DayPhase:
        """act_time まで待機 → prepare/act → close_time まで監視 → finish."""
        rt = self._rt
        if self.bootstrap() is None:
            return DayPhase.HALTED
        schedule = rt.settings.engine
        act_at = _combine(trade_date, schedule.act_time)
        close_at = _combine(trade_date, schedule.close_time)
        if self._wait_until(act_at):
            try:
                if rt.engine.prepare(trade_date):
                    rt.engine.act(trade_date)
            except KillSwitchActiveError:
                pass  # engine 内で通知済み・phase=HALTED
        if rt.engine.phase is DayPhase.MONITORING:
            self._monitor_until(close_at, schedule.monitor_interval_s)
        rt.engine.finish(trade_date)
        return rt.engine.phase

    # ------------------------------------------------------------------ #

    def _monitor_until(self, close_at: datetime, interval_s: float) -> None:
        rt = self._rt
        while not self._stopping:
            if self._now() >= close_at:
                return
            try:
                rt.engine.monitor()
            except Exception as exc:  # 監視1周の失敗で当日を終わらせない
                rt.notifier.notify(Severity.ALERT, "監視の1周に失敗（再試行します）", repr(exc))
            remaining = (close_at - self._now()).total_seconds()
            if remaining <= 0:
                return
            if not self._sleep_chunked(min(interval_s, remaining)):
                return

    def _wait_until(self, target: datetime) -> bool:
        """目標時刻まで待機する。中断（Ctrl+C）なら False."""
        while not self._stopping:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return True
            if not self._sleep_chunked(remaining):
                return False
        return False

    def _sleep_chunked(self, total_s: float) -> bool:
        remaining = total_s
        while remaining > 0:
            try:
                self._sleep(min(remaining, _WAIT_CHUNK_S))
            except KeyboardInterrupt:
                self._stopping = True
                self._rt.notifier.notify(
                    Severity.ALERT, "終了シグナルを受け取り終日処理へ移行します"
                )
                return False
            remaining -= _WAIT_CHUNK_S
        return True

    def _now(self) -> datetime:
        return self._clock()

    def _today(self) -> date:
        return self._clock().date()


def _combine(trade_date: date, hhmm: str) -> datetime:
    return datetime.combine(trade_date, dt_time.fromisoformat(hhmm))


def _price_fn(client: KabuClient) -> Callable[[str], float | None]:
    """板情報の現在値（東証プライム市場既定）."""

    def price_fn(symbol: str) -> float | None:
        result = client.board(symbol)
        if isinstance(result, ApiSuccess) and result.data.CurrentPrice is not None:
            return float(result.data.CurrentPrice)
        return None

    return price_fn


def _positions_fn(client: KabuClient) -> Callable[[], list[PositionItem]]:
    def positions_fn() -> list[PositionItem]:
        result = client.positions()
        if isinstance(result, ApiSuccess):
            return list(result.data.Holdings)
        return []

    return positions_fn


def _profit_fn(client: KabuClient) -> Callable[[], float]:
    """建玉の評価損益合計（取得失敗時は0=キルスイッチ判定しない）."""

    def profit_fn() -> float:
        result = client.positions()
        if not isinstance(result, ApiSuccess):
            return 0.0
        return sum(float(h.ProfitLoss or 0.0) for h in result.data.Holdings)

    return profit_fn
