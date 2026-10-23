"""⑥ 1営業日エンジン: データ→戦略→リスク→注文→監視→終値の状態機械.

DESIGN §3.4 の原則:
- 「1営業日 = 1プロセス」。早朝強制ログアウト後の日次フル再認証を前提に、
  `bootstrap()`（認証 + 起動時リコンサイル）から `finish()` までを1回だけ回す
- 戦略はシグナルしか出さない。サイジング・発注・リスク判定はここで行う
- 当日バー欠損は設定 `halt_when_today_bar_missing` に従い取引中断（TESTING §3.4）
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from enum import StrEnum

from strata.api.models import OrderSide, PositionItem, PriceType, SendOrderRequest
from strata.config import DataSettings, WatchlistEntry
from strata.data.base import DailyBar, MarketDataProvider
from strata.data.store import BarRepository
from strata.data.validate import validate_bars
from strata.notify.base import Notifier, Severity
from strata.order.manager import KillSwitchActiveError, OrderManager, OrderManagerError
from strata.order.repository import IntentRepository
from strata.risk.checks import RiskEngine
from strata.risk.killswitch import KillSwitch
from strata.store.db import utc_now_iso
from strata.strategy.base import Signal, SignalAction, Strategy


class DayPhase(StrEnum):
    NEW = "NEW"
    #: データ検証済み・発注前
    PREPARED = "PREPARED"
    #: シグナル執行済み（売買終了後の監視もここ）
    MONITORING = "MONITORING"
    FINISHED = "FINISHED"
    #: データ品質・リスク上の理由で本日の取引を中断
    HALTED = "HALTED"


#: 監視価格で判定するロスカットライン {symbol: ロスカット価格}
StopLevels = dict[str, float]


class DailyEngine:
    """1営業日のオーケストレーション（依存はすべて注入）.

    本番配線: provider=make_provider(settings), manager=OrderManager(+kill),
    price_fn=lambda s: kabu.board(s) の現在値, positions_fn=kabu.positions(),
    profit_fn=建玉評価損合計。テストはフェイクを注入。
    """

    def __init__(
        self,
        *,
        provider: MarketDataProvider | None = None,
        strategy: Strategy,
        universe: Sequence[WatchlistEntry],
        manager: OrderManager,
        risk: RiskEngine,
        repo: IntentRepository,
        notifier: Notifier,
        settings: DataSettings,
        bars_repo: BarRepository | None = None,
        kill_switch: KillSwitch | None = None,
        price_fn: Callable[[str], float | None] = lambda _s: None,
        positions_fn: Callable[[], Sequence[PositionItem]] = lambda: (),
        profit_fn: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._strategy = strategy
        self._universe = list(universe)
        self._manager = manager
        self._risk = risk
        self._repo = repo
        self._notifier = notifier
        self._settings = settings
        self._bars_repo = bars_repo
        self._kill = kill_switch
        self._price_fn = price_fn
        self._positions_fn = positions_fn
        self._profit_fn = profit_fn
        self._clock = clock
        self.phase = DayPhase.NEW
        self.halted_reason: str | None = None
        self._bars: dict[str, list[DailyBar]] = {}
        self._trade_date: date | None = None
        self.stops: StopLevels = {}

    # ------------------------------------------------------------------ #

    def prepare(self, trade_date: date) -> bool:
        """全銘柄の日足を取得・検証し、中断判定を通す.

        Returns:
            取引続行なら True。False の場合 phase=HALTED で act は不実行。
        """
        self._require_new()
        self._trade_date = trade_date
        provider = self._provider
        if provider is None:
            return self._halt(trade_date, "市場データプロバイダが設定されていません")
        bars: dict[str, list[DailyBar]] = {}
        try:
            history_start = date.fromisoformat(self._settings.history_start)
        except ValueError as exc:  # pragma: no cover - 設定検証で防止済み
            raise ValueError(f"history_start が不正です: {self._settings.history_start}") from exc

        for entry in self._universe:
            raw = provider.fetch_daily(entry.symbol, history_start, trade_date)
            report = validate_bars(raw, today=trade_date)
            if self._bars_repo is not None:
                self._bars_repo.upsert_bars(report.valid)
            for issue in report.rejected:
                self._notifier.notify(
                    Severity.ALERT,
                    f"[データ却下] {entry.symbol} {issue.bar.date}",
                    "; ".join(issue.reasons),
                )
            if not report.valid:
                return self._halt(trade_date, f"{entry.symbol}: 有効なバーが1本もありません")
            if report.missing_today and self._settings.halt_when_today_bar_missing:
                return self._halt(
                    trade_date,
                    f"{entry.symbol}: 当日バー欠損（直近有効バー {report.latest_date}）",
                )
            if report.missing_today:
                self._notifier.notify(
                    Severity.ALERT,
                    f"[データ警告] {entry.symbol} 当日バーなし",
                    f"直近有効バー {report.latest_date} で継続します",
                )
            bars[entry.symbol] = report.valid
        self._bars = bars
        self.phase = DayPhase.PREPARED
        return True

    def act(self, trade_date: date) -> list[Signal]:
        """戦略シグナルを生成し、サイジングして意図台帳経由で発注する."""
        if self.phase is not DayPhase.PREPARED:
            raise RuntimeError(f"act は PREPARED でのみ実行できます: {self.phase}")
        signals = self._strategy.generate_signals(self._bars)
        for signal in signals:
            try:
                self._execute(signal, trade_date)
            except KillSwitchActiveError as exc:
                self._notifier.notify(Severity.CRITICAL, "[発注停止] キルスイッチ", str(exc))
                self.phase = DayPhase.HALTED
                self.halted_reason = str(exc)
                raise
            except OrderManagerError as exc:
                # 個別シグナルの拒否（リスク・重複・クロス）は継続してよい
                self._notifier.notify(
                    Severity.ALERT,
                    f"[シグナル見送り] {signal.symbol} {signal.action}",
                    str(exc),
                )
        self.phase = DayPhase.MONITORING
        return signals

    def monitor(self) -> list[str]:
        """発注済み・保有中の監視。日次損失判定とロスカット執行.

        Returns:
            ロスカット執行した銘柄リスト
        """
        if self.phase is not DayPhase.MONITORING:
            return []
        # 約定状態をブローカーから最新化（この同期が無いと空売り規制ならぬクロス判定で
        # 自らの BUY が売りを妨げる）
        for open_date in sorted({i.trade_date for i in self._repo.list_open_intents()}):
            self._manager.poll(open_date)
        if self._profit_fn is not None and self._kill is not None:
            pl = self._profit_fn()
            if self._kill.check_daily_pl(pl):
                self._notifier.notify(
                    Severity.CRITICAL,
                    "[キルスイッチ] 日次損失到達",
                    f"本日の評価損益 {pl:,.0f} 円。以後の発注は停止済みです",
                )
        triggered: list[str] = []
        for position in self._positions_fn():
            symbol = str(position.Symbol or "")
            stop = self.stops.get(symbol)
            qty = int(position.HoldQty or 0)
            if stop is None or qty <= 0:
                continue
            price = self._price_fn(symbol)
            if price is None or price > stop:
                continue
            try:
                self._liquidate(symbol, qty, reason=f"ロスカット ({price} <= {stop})")
            except OrderManagerError as exc:
                self._notifier.notify(Severity.ALERT, f"[ロスカット失敗] {symbol}", str(exc))
                continue
            triggered.append(symbol)
        return triggered

    def finish(self, trade_date: date) -> None:
        """日終り処理: 建玉スナップショット保存と日次サマリ通知."""
        if self.phase in (DayPhase.FINISHED,):
            return
        positions = list(self._positions_fn())
        captured_at = utc_now_iso()
        self._repo.add_positions_snapshot(
            captured_at,
            [
                {
                    "Symbol": p.Symbol,
                    "Exchange": p.Exchange,
                    "Side": p.Side,
                    "HoldQty": p.HoldQty,
                    "Price": p.Price,
                }
                for p in positions
            ],
        )
        summary = (
            f"phase={self.phase.value} 中断={'はい' if self.halted_reason else 'いいえ'}\n"
            f"建玉 {len(positions)} 銘柄 / 監視 {len(self._universe)} 銘柄"
        )
        if self.halted_reason:
            summary += f"\n中断理由: {self.halted_reason}"
        self._notifier.notify(Severity.INFO, f"[日次サマリ] {trade_date}", summary)
        self.phase = DayPhase.FINISHED

    def run_day(self, trade_date: date) -> DayPhase:
        """prepare→act→finish の通し実行（監視は別スケジュールで monitor()）."""
        if self.prepare(trade_date):
            self.act(trade_date)
        self.finish(trade_date)
        return self.phase

    # ------------------------------------------------------------------ #

    def _execute(self, signal: Signal, trade_date: date) -> None:
        entry = next((e for e in self._universe if e.symbol == signal.symbol), None)
        if entry is None:
            return
        if signal.action is SignalAction.BUY:
            self._buy(signal, entry, trade_date)
        else:
            self._sell(signal, entry, trade_date)

    def _buy(self, signal: Signal, entry: WatchlistEntry, trade_date: date) -> None:
        stop_width = entry.stop_width
        if stop_width is None:
            raise OrderManagerError(
                f"{entry.symbol}: stop_width 未設定のためリスク評価できません（設定必須）"
            )
        closes = [b.close for b in self._bars.get(entry.symbol, [])]
        ref_price = closes[-1] if closes else 0.0
        if ref_price <= 0:
            return
        qty = self._size(entry, stop_width, trade_date)
        if qty <= 0:
            self._notifier.notify(
                Severity.ALERT,
                f"[サイジング0] {entry.symbol}",
                "リスク予算が1単元に満たないため発注を見送ります",
            )
            return
        request = SendOrderRequest(
            symbol=entry.symbol, side=OrderSide.BUY, qty=qty, price_type=PriceType.MARKET
        )
        self._manager.submit(
            request, trade_date=trade_date, ref_price=ref_price, stop_width=stop_width
        )
        self.stops[entry.symbol] = ref_price - stop_width

    def _sell(self, signal: Signal, entry: WatchlistEntry, trade_date: date) -> None:
        held = sum(
            int(p.HoldQty or 0)
            for p in self._positions_fn()
            if str(p.Symbol) == entry.symbol and p.Side == "2"
        )
        if held < entry.trading_unit:
            return
        closes = [b.close for b in self._bars.get(entry.symbol, [])]
        self._liquidate(
            entry.symbol,
            held,
            reason=f"シグナル: {signal.reason}",
            ref=closes[-1] if closes else 0.0,
            trade_date=trade_date,
        )

    def _liquidate(
        self,
        symbol: str,
        qty: int,
        *,
        reason: str,
        ref: float = 0.0,
        trade_date: date | None = None,
    ) -> None:
        request = SendOrderRequest(
            symbol=symbol, side=OrderSide.SELL, qty=qty, price_type=PriceType.MARKET
        )
        date_arg = trade_date or self._trade_date or self._today()
        try:
            self._manager.submit(request, trade_date=date_arg, ref_price=ref)
        finally:
            self.stops.pop(symbol, None)
        self._notifier.notify(Severity.ALERT, f"[売り執行] {symbol} {qty}株", reason)

    def _size(self, entry: WatchlistEntry, stop_width: float, trade_date: date) -> int:
        """1銘柄分のリスク予算 = min(枠均等割り, 日次許容リスクの残り)."""
        risk_settings = self._risk.settings
        per_slot = risk_settings.max_total_risk / max(1, risk_settings.max_open_positions)
        remaining = risk_settings.max_total_risk - self._repo.sum_committed_risk(
            trade_date.isoformat()
        )
        budget = max(0.0, min(per_slot, remaining))
        qty = self._risk.size_position(budget, stop_width, entry.trading_unit)
        cap = risk_settings.max_qty_per_order
        if qty > cap:
            qty = cap // entry.trading_unit * entry.trading_unit
        return qty

    def _halt(self, trade_date: date, reason: str) -> bool:
        self.phase = DayPhase.HALTED
        self.halted_reason = reason
        self._notifier.notify(
            Severity.ALERT,
            f"[取引中断] {trade_date}",
            f"{reason}\n本日の新規発注は行いません（halt_when_today_bar_missing="
            f"{self._settings.halt_when_today_bar_missing}）",
        )
        return False

    def _today(self) -> date:
        return (self._clock or (lambda: datetime.now()))().date()

    def _require_new(self) -> None:
        if self.phase is not DayPhase.NEW:
            raise RuntimeError(f"1プロセス1営業日の原則に反します: phase={self.phase}")
