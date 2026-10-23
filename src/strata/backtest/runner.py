"""日足リプレイ型バックテストランナー.

DESIGN §1.3 / TESTING §3.4 の要求: バックテストは運用と**同一の
リスク・注文コードパス**（IntentRepository + OrderManager + RiskEngine）
を通す。ブローカーのみ PaperBroker に差し替える。

既知の近似（正直さのための注記）:
- 約定気配は終値（NEXT_OPEN は「翌営業日終値」で約定する近似）
- 手数料・スリッページなし（実運用ゲートではペーパートレードで実測）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from strata.api.models import OrderSide, PriceType, SendOrderRequest
from strata.backtest.broker import FillPolicy, PaperBroker
from strata.backtest.metrics import EquityPoint, compute_metrics
from strata.config import RiskSettings, WatchlistEntry
from strata.data.base import DailyBar
from strata.notify.base import InMemoryNotifier, Notifier
from strata.order.manager import OrderManager, OrderManagerError
from strata.order.repository import IntentRepository
from strata.risk.checks import RiskEngine
from strata.store.db import init_db
from strata.strategy.base import Signal, SignalAction, Strategy


@dataclass(frozen=True)
class BacktestSettings:
    initial_cash: float = 1_000_000.0
    fill: FillPolicy = FillPolicy.NEXT_OPEN
    #: WatchlistEntry.stop_width 未指定時の既定ロスカット幅（1株あたり）
    default_stop_width: float = 20.0


@dataclass
class BacktestResult:
    equity_curve: list[EquityPoint] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    metrics: Mapping[str, float] = field(default_factory=dict)
    #: リスク拒否・重複などで発注に至らなかったシグナル数
    rejected_signals: int = 0


class BacktestRunner:
    """[日付] × [銘柄] で戦略→リスク→意図台帳→仮定約定を回す."""

    def __init__(
        self,
        *,
        strategy: Strategy,
        universe: Sequence[WatchlistEntry],
        bars: Mapping[str, Sequence[DailyBar]],
        risk_settings: RiskSettings | None = None,
        settings: BacktestSettings | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._strategy = strategy
        self._universe = {e.symbol: e for e in universe}
        self._bars = {
            symbol: sorted(series, key=lambda b: b.date) for symbol, series in bars.items()
        }
        self._risk_settings = risk_settings or RiskSettings()
        self._settings = settings or BacktestSettings()
        self._notifier = notifier or InMemoryNotifier()

    def run(self) -> BacktestResult:
        conn = init_db(Path(":memory:"))
        try:
            return self._run(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------------ #

    def _run(self, conn: sqlite3.Connection) -> BacktestResult:
        repo = IntentRepository(conn)
        broker = PaperBroker(initial_cash=self._settings.initial_cash, fill=self._settings.fill)
        risk = RiskEngine(self._risk_settings)
        manager = OrderManager(repo, broker, self._notifier, risk_engine=risk)  # type: ignore[arg-type]

        dates = sorted({b.date for series in self._bars.values() for b in series})
        closes = self._close_lookup()
        result = BacktestResult()

        for day in dates:
            today_closes = {s: closes[(s, day)] for s in self._bars if (s, day) in closes}
            broker.set_market_day(day, today_closes)
            # 翌日約定(NEXT_OPEN)で意図の営業日が当日とずれ得るため、
            # 未完了意図の日付を横断的に同期する
            for open_date in sorted({i.trade_date for i in repo.list_open_intents()}):
                manager.poll(open_date)

            signals = self._strategy.generate_signals(self._bars_upto(day))
            result.signals.extend(signals)
            for signal in signals:
                if not self._execute(signal, day, broker, manager, repo, risk):
                    result.rejected_signals += 1

            market_value = sum(
                qty * (closes.get((symbol, day)) or broker.price_of(symbol))
                for symbol, qty in broker.holdings.items()
                if qty > 0
            )
            equity = broker.cash + market_value
            result.equity_curve.append(
                EquityPoint(date=day, cash=broker.cash, market_value=market_value, equity=equity)
            )

        executed = conn.execute("SELECT COUNT(*) AS n FROM executions").fetchone()["n"]
        metrics = compute_metrics(
            result.equity_curve,
            initial_cash=self._settings.initial_cash,
            executions=int(executed),
            round_trips=broker.round_trips,
            winning_exits=broker.winning_exits,
            realized_pnl=broker.realized_pnl,
            rejected=result.rejected_signals,
        )
        result.metrics = dict(metrics.values)
        return result

    def _execute(
        self,
        signal: Signal,
        day: str,
        broker: PaperBroker,
        manager: OrderManager,
        repo: IntentRepository,
        risk: RiskEngine,
    ) -> bool:
        entry = self._universe.get(signal.symbol)
        if entry is None:
            return False
        stop_width = entry.stop_width or self._settings.default_stop_width
        try:
            if signal.action is SignalAction.BUY:
                ref_price = self._latest_close(signal.symbol, day)
                if ref_price is None or ref_price <= 0:
                    return False
                qty = self._size(signal, day, repo, risk, entry, stop_width)
                if qty <= 0:
                    return False
                request = SendOrderRequest(
                    symbol=signal.symbol,
                    side=OrderSide.BUY,
                    qty=qty,
                    price_type=PriceType.MARKET,
                )
                manager.submit(request, trade_date=day, ref_price=ref_price, stop_width=stop_width)
            else:
                held = broker.held_qty(signal.symbol)
                if held < entry.trading_unit:
                    return False
                request = SendOrderRequest(
                    symbol=signal.symbol,
                    side=OrderSide.SELL,
                    qty=held,
                    price_type=PriceType.MARKET,
                )
                manager.submit(
                    request,
                    trade_date=day,
                    ref_price=self._latest_close(signal.symbol, day) or 0.0,
                )
        except OrderManagerError:
            # リスク拒否・重複・クロス・リジェクト。戦略ループは止めずに計上
            return False
        return True

    def _size(
        self,
        signal: Signal,
        day: str,
        repo: IntentRepository,
        risk: RiskEngine,
        entry: WatchlistEntry,
        stop_width: float,
    ) -> int:
        per_slot = self._risk_settings.max_total_risk / max(
            1, self._risk_settings.max_open_positions
        )
        remaining = self._risk_settings.max_total_risk - repo.sum_committed_risk(day)
        budget = max(0.0, min(per_slot, remaining))
        qty = risk.size_position(budget, stop_width, entry.trading_unit)
        # 1回数量上限でクランプ（超過はリスク側で確実拒否になるため事前に合わせる）
        cap = self._risk_settings.max_qty_per_order
        if qty > cap:
            qty = cap // entry.trading_unit * entry.trading_unit
        return qty

    # ------------------------------------------------------------------ #

    def _close_lookup(self) -> dict[tuple[str, str], float]:
        return {(b.symbol, b.date): b.close for series in self._bars.values() for b in series}

    def _bars_upto(self, day: str) -> dict[str, list[DailyBar]]:
        out: dict[str, list[DailyBar]] = {}
        for symbol, series in self._bars.items():
            bars = [b for b in series if b.date <= day]
            if bars:
                out[symbol] = bars
        return out

    def _latest_close(self, symbol: str, day: str) -> float | None:
        closes = [b.close for b in self._bars.get(symbol, []) if b.date <= day]
        return closes[-1] if closes else None
