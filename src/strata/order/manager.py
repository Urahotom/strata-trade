"""注文ライフサイクル管理（Intent Log・突合・リコンサイル）.

設計の核心（DESIGN.md §3.2）:
- 発注前に意図をPLANNEDでコミット → SENTへ更新してから送信（Write-Ahead）
- 送信結果不確定（通信失敗/応答破損）時は **絶対に自動再送せず**
  /orders 突合で検知する。未到着が確認できて初めて PLANNED に巻き戻し
- リジェクトは終端として記録し、同一意図の自動再発注は行わない
- 起動時リコンサイルでオーファン建玉を検知したらキルスイッチ作動
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from strata.api.client import KabuClient
from strata.api.models import (
    OrderItem,
    OrderSide,
    SendOrderRequest,
)
from strata.api.results import ApiFailure, ApiSuccess, HttpFailure
from strata.notify.base import Notifier, Severity
from strata.order.repository import Intent, IntentRepository
from strata.order.states import OrderState
from strata.risk.checks import RiskContext, RiskEngine, RiskVerdict, SubmitSpec
from strata.risk.killswitch import KillSwitch
from strata.store.db import utc_now_iso

logger = logging.getLogger(__name__)

_EPS = 1e-9


class PartialFillPolicy(StrEnum):
    #: 部分約定のまま終了している注文は残余を取消する（既定）
    CANCEL_REMAINDER = "CANCEL_REMAINDER"
    #: 何もせず状態のみ反映
    HOLD = "HOLD"


class OrderManagerError(Exception):
    """注文管理のエラー基底."""


class DuplicateIntentError(OrderManagerError):
    """同一銘柄・同一方向の非終端意図が既に存在する（重複発注防止）."""

    def __init__(self, symbol: str, side: OrderSide, existing_ids: list[str]) -> None:
        super().__init__(f"重複発注を阻止しました: {symbol} {side} (既存意図: {existing_ids})")
        self.symbol = symbol
        self.side = side
        self.existing_ids = existing_ids


class CrossOrderError(OrderManagerError):
    """同一銘柄の反対方向の未完了意図が存在（クロス取引のローカル防止）."""

    def __init__(self, symbol: str, existing_ids: list[str]) -> None:
        super().__init__(
            f"クロス取引相当の発注を阻止しました: {symbol} (反対方向の意図: {existing_ids})"
        )
        self.symbol = symbol
        self.existing_ids = existing_ids


class KillSwitchActiveError(OrderManagerError):
    """キルスイッチ作動中は発注不可."""


class RiskBlockedError(OrderManagerError):
    """発注前リスクチェックで拒否された（意図は作成されていない）."""

    def __init__(self, verdict: RiskVerdict) -> None:
        super().__init__("リスクチェックで拒否: " + "; ".join(verdict.reasons))
        self.verdict = verdict


@dataclass
class StartupReport:
    """起動時リコンサイルの結果."""

    open_intents: list[Intent] = field(default_factory=list)
    orphan_positions: list[dict[str, Any]] = field(default_factory=list)
    snapshot_captured_at: str | None = None
    positions_available: bool = False


def _opposite(side: OrderSide) -> OrderSide:
    return OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY


class OrderManager:
    """発注・突合・状態同期の唯一の経路."""

    def __init__(
        self,
        repo: IntentRepository,
        client: KabuClient,
        notifier: Notifier,
        *,
        risk_engine: RiskEngine | None = None,
        kill_switch: KillSwitch | None = None,
        partial_fill_policy: PartialFillPolicy = PartialFillPolicy.CANCEL_REMAINDER,
    ) -> None:
        self._repo = repo
        self._client = client
        self._notifier = notifier
        self._risk = risk_engine
        self._kill = kill_switch
        self._policy = partial_fill_policy

    # ------------------------------------------------------------------
    # 発注
    # ------------------------------------------------------------------

    def submit(
        self,
        request: SendOrderRequest,
        *,
        trade_date: date | str,
        ref_price: float,
        stop_width: float | None = None,
    ) -> Intent:
        """意図台帳経由で発注する。リスク拒否・重複・クロスは意図作成前に拒否."""
        self._check_kill_switch()
        date_str = trade_date.isoformat() if isinstance(trade_date, date) else trade_date

        est_risk = self._risk_check(request, date_str, ref_price, stop_width)
        self._check_no_conflict(date_str, request.symbol, request.side)

        intent = Intent.new(
            trade_date=date_str,
            symbol=request.symbol,
            market=request.exchange,
            side=request.side,
            qty=request.qty,
            price_type=request.price_type,
            price=request.price or None,
            request_json=json.dumps(request.to_payload(), ensure_ascii=False),
        )
        self._repo.create(intent)
        if est_risk is not None:
            self._repo.append_event(intent.intent_id, "RISK_APPROVED", {"est_risk": est_risk})
        return self._send(intent, request)

    def retry(self, intent_id: str, *, ref_price: float, stop_width: float | None = None) -> Intent:
        """PLANNED へ巻き戻った意図を再送する（突合で未到着確認済みのもののみ）."""
        self._check_kill_switch()
        intent = self._require(intent_id)
        if intent.state is not OrderState.PLANNED:
            raise OrderManagerError(f"PLANNED の意図のみ再送できます: {intent_id} ({intent.state})")
        request = self._request_from(intent)
        active = [
            i.intent_id
            for i in self._repo.find_active(
                trade_date=intent.trade_date, symbol=intent.symbol, side=intent.side
            )
            if i.intent_id != intent.intent_id
        ]
        if active:
            raise DuplicateIntentError(intent.symbol, intent.side, active)
        opp = [
            i.intent_id
            for i in self._repo.find_active(
                trade_date=intent.trade_date, symbol=intent.symbol, side=_opposite(intent.side)
            )
            if i.intent_id != intent.intent_id
        ]
        if opp:
            raise CrossOrderError(intent.symbol, opp)
        # 発注前リスクは再評価（price等の基準は submit 時 ref_price を再利用）
        self._risk_check(request, intent.trade_date, ref_price, stop_width)
        return self._send(intent, request)

    def cancel(self, intent_id: str, *, reason: str = "") -> bool:
        """未完了注文の取消を要求する。状態は poll で確定する."""
        self._check_kill_switch()
        intent = self._require(intent_id)
        if intent.order_id is None:
            raise OrderManagerError(f"OrderId 未確定の意図は取消できません: {intent_id}")
        result = self._client.cancelorder(intent.order_id)
        if isinstance(result, ApiSuccess):
            self._repo.append_event(intent.intent_id, "CANCEL_REQUESTED", {"reason": reason})
            self._note_success()
            return True
        self._handle_failure_events(intent, result, "CANCEL_FAILED")
        return False

    # ------------------------------------------------------------------
    # 突合（送信結果不確定の確定）
    # ------------------------------------------------------------------

    def reconcile(self, intent: Intent) -> Intent:
        """SENT 意図がブローカーに到達したかを /orders で判定する.

        - 到達発見 → ACK（自動再送は発生していないため二重発注にならない）
        - 未到着確定 → PLANNED へ巻き戻し（retry() で再送可能になる）
        """
        if intent.state is not OrderState.SENT:
            return intent
        result = self._client.orders(symbol=intent.symbol)
        if not isinstance(result, ApiSuccess):
            # 突合自体が失敗した → SENT のまま（次回再試行、自動再送はしない）
            self._repo.append_event(intent.intent_id, "RECONCILE_FAILED", {"reason": repr(result)})
            if isinstance(result, HttpFailure | ApiFailure):
                self._note_success()
            else:
                self._note_network_failure()
            return intent
        self._note_success()
        match = self._match_broker_order(intent, result.data)
        if match is not None:
            updated = self._repo.transition(
                intent.intent_id,
                OrderState.ACK,
                event_type="RECONCILE_FOUND",
                order_id=match.ID,
                detail={"recv_time": match.RecvTime},
            )
            self._notifier.notify(
                Severity.ALERT,
                f"応答喪失したが突合で注文を復旧: {intent.symbol} {intent.side}",
                f"OrderId={match.ID}",
            )
            return updated
        return self._repo.transition(
            intent.intent_id,
            OrderState.PLANNED,
            event_type="RECONCILE_NOT_FOUND",
        )

    # ------------------------------------------------------------------
    # 状態同期
    # ------------------------------------------------------------------

    def poll(self, trade_date: date | str) -> list[Intent]:
        """非終端意図すべてをブローカー状態へ同期する."""
        date_str = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
        updated: list[Intent] = []
        for intent in self._repo.list_open(date_str):
            result = self.sync_one(intent)
            if result is not None:
                updated.append(result)
        return updated

    def sync_one(self, intent: Intent) -> Intent | None:
        """1件の意図を /orders と突合して状態を前進させる。変化時その意図を返す."""
        if intent.state is OrderState.PLANNED:
            return None
        if intent.state is OrderState.SENT:
            return self.reconcile(intent)
        if intent.order_id is None:  # pragma: no cover - ACK以降は必ず確定済み
            return None
        result = self._client.orders(order_id=intent.order_id)
        if not isinstance(result, ApiSuccess):
            self._note_failure_from(result)
            return None
        self._note_success()
        if not result.data:
            self._repo.append_event(intent.intent_id, "POLL_ORDER_MISSING")
            return None
        return self._advance_from_broker(intent, result.data[0])

    def startup_reconcile(self, trade_date: date | str) -> StartupReport:
        """起動時リコンサイル: 未完了意図の突合 + 建玉との照合.

        DBに根拠のない建玉（オーファン）を検知したらキルスイッチを作動させ、
        以後の発注を停止する。自動解消は行わない（人間判断）。
        """
        date_str = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
        report = StartupReport()

        for intent in self._repo.list_open(date_str):
            synced = self.sync_one(intent)
            current = synced if synced is not None else intent
            report.open_intents.append(current)
            if current.state is OrderState.SENT:
                self._notifier.notify(
                    Severity.ALERT,
                    f"起動時リコンサイル: 送信未確定の意図が残存 {current.symbol}",
                    f"intent_id={current.intent_id}（自動再送は行いません）",
                )

        pos_result = self._client.positions()
        if not isinstance(pos_result, ApiSuccess):
            self._notifier.notify(
                Severity.ALERT, "起動時リコンサイル: 建玉照会に失敗しました（続行）"
            )
            return report

        holdings = [
            {
                "Symbol": h.Symbol,
                "Exchange": h.Exchange,
                "Side": h.Side,
                "HoldQty": h.HoldQty,
                "Price": h.Price,
            }
            for h in pos_result.data.Holdings
            if (h.HoldQty or 0) > 0
        ]
        report.positions_available = True
        report.snapshot_captured_at = utc_now_iso()
        self._repo.add_positions_snapshot(report.snapshot_captured_at, holdings)

        known = self._known_position_symbols(date_str)
        report.orphan_positions = [h for h in holdings if h["Symbol"] not in known]
        if report.orphan_positions:
            symbols = ", ".join(str(h["Symbol"]) for h in report.orphan_positions)
            if self._kill is not None:
                self._kill.activate(f"オーファン建玉を検知: {symbols}")
            self._notifier.notify(
                Severity.CRITICAL,
                "オーファン建玉を検知しました（全発注停止）",
                "DBに意図のない建玉が見つかりました。自動解消は行いません:\n"
                + json.dumps(report.orphan_positions, ensure_ascii=False, indent=2),
            )
        return report

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _check_kill_switch(self) -> None:
        if self._kill is not None and self._kill.active:
            raise KillSwitchActiveError(
                "キルスイッチ作動中のため発注できません: " + "; ".join(self._kill.reasons)
            )

    @staticmethod
    def _request_from(intent: Intent) -> SendOrderRequest:
        """台帳の意図から送信済みリクエストを復元する（再送用）."""
        return SendOrderRequest(
            symbol=intent.symbol,
            exchange=intent.market,
            side=intent.side,
            qty=intent.qty,
            price_type=intent.price_type,
            price=intent.price or 0.0,
        )

    def _risk_check(
        self,
        request: SendOrderRequest,
        date_str: str,
        ref_price: float,
        stop_width: float | None,
    ) -> float | None:
        if self._risk is None:
            return None
        spec = SubmitSpec(
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            price=request.price,
            ref_price=ref_price,
            stop_width=stop_width,
        )
        ctx = RiskContext(
            open_positions=self._repo.count_open_symbols(date_str),
            committed_risk=self._repo.sum_committed_risk(date_str),
        )
        verdict = self._risk.check(spec, ctx)
        if not verdict.ok:
            raise RiskBlockedError(verdict)
        return request.qty * stop_width if stop_width else None

    def _check_no_conflict(self, date_str: str, symbol: str, side: OrderSide) -> None:
        same = [
            i.intent_id
            for i in self._repo.find_active(trade_date=date_str, symbol=symbol, side=side)
        ]
        if same:
            raise DuplicateIntentError(symbol, side, same)
        opp = [
            i.intent_id
            for i in self._repo.find_active(
                trade_date=date_str, symbol=symbol, side=_opposite(side)
            )
        ]
        if opp:
            raise CrossOrderError(symbol, opp)

    def _send(self, intent: Intent, request: SendOrderRequest) -> Intent:
        self._repo.transition(intent.intent_id, OrderState.SENT, event_type="SENT")
        result = self._client.sendorder(request)
        if isinstance(result, ApiSuccess):
            order_id = result.data.OrderId or ""
            updated = self._repo.transition(
                intent.intent_id,
                OrderState.ACK,
                event_type="ORDER_ACCEPTED",
                order_id=order_id,
                response_json=json.dumps(
                    {"Result": result.data.Result, "OrderId": result.data.OrderId}
                ),
            )
            self._notifier.notify(
                Severity.INFO,
                f"注文受付: {intent.symbol} {intent.side} x{intent.qty}",
                f"OrderId={order_id} intent={intent.intent_id}",
            )
            self._note_success()
            return updated
        if isinstance(result, ApiFailure) and result.is_rejection:
            updated = self._repo.transition(
                intent.intent_id,
                OrderState.REJECTED,
                event_type="ORDER_REJECTED",
                reject_code=result.code,
                reject_message=result.message,
            )
            self._notifier.notify(
                Severity.ALERT,
                f"注文リジェクト: {intent.symbol} {intent.side} x{intent.qty}",
                f"Result={result.code} {result.message}（自動再発注は行いません）",
            )
            self._note_success()
            return updated
        # それ以外（HTTP/通信/解析失敗）は「送信済み可能性あり」= 突合対象
        self._handle_failure_events(intent, result, "SEND_UNCONFIRMED")
        current = self._require(intent.intent_id)
        return self.reconcile(current)

    def _handle_failure_events(self, intent: Intent, result: object, event: str) -> None:
        detail = {"reason": repr(result)}
        self._repo.append_event(intent.intent_id, event, detail)
        if isinstance(result, HttpFailure | ApiFailure):
            self._note_success()
        else:
            self._note_network_failure()
        self._notifier.notify(
            Severity.ALERT,
            f"発注結果不明 ({event}): {intent.symbol} {intent.side} x{intent.qty}",
            str(detail["reason"]) + "（自動再送せず突合します）",
        )

    def _match_broker_order(self, intent: Intent, orders: list[OrderItem]) -> OrderItem | None:
        claimed = self._repo.claimed_order_ids()
        candidates: list[OrderItem] = []
        for o in orders:
            if o.ID is None or o.ID in claimed:
                continue
            if o.Symbol != intent.symbol:
                continue
            if o.Side != intent.side.api_value:
                continue
            if (o.OrderQty or 0) != intent.qty:
                continue
            if intent.price and abs((o.Price or 0.0) - intent.price) > _EPS:
                continue
            candidates.append(o)
        if not candidates:
            return None
        # 直近受信順（RecvTime 降順）。同名条件の取り違えは同一時間窓では稀
        return max(candidates, key=lambda o: o.RecvTime or "")

    def _advance_from_broker(self, intent: Intent, item: OrderItem) -> Intent | None:
        current = intent
        self._record_fills(current, item)

        if not item.is_terminal:
            if current.state is OrderState.ACK and (item.State or 0) >= 3:
                current = self._repo.transition(
                    current.intent_id, OrderState.WORKING, event_type="ORDER_WORKING"
                )
            cum = item.cum_qty
            if 0 < cum < item.order_qty:
                if current.state is OrderState.WORKING:
                    current = self._repo.transition(
                        current.intent_id,
                        OrderState.PART_FILLED,
                        event_type="ORDER_PARTIAL_FILL",
                        detail={"cum_qty": cum, "order_qty": item.order_qty},
                    )
                if self._policy is PartialFillPolicy.CANCEL_REMAINDER:
                    self._request_cancel_remainder(current, item)
            elif cum >= item.order_qty > 0:
                if current.state in (OrderState.WORKING, OrderState.PART_FILLED):
                    current = self._repo.transition(
                        current.intent_id, OrderState.FILLED, event_type="ORDER_FILLED"
                    )
                    self._notify_fill(current, item, partial=False)
            return current if current != intent else None

        # 終端（State=5）: 約定状況と明細から終端状態を決定
        target = self._terminal_state(item)
        path_event = "ORDER_TERMINAL"
        if current.state in (OrderState.ACK, OrderState.SENT):
            current = self._repo.transition(
                current.intent_id, OrderState.WORKING, event_type="ORDER_WORKING"
            )
        updated = self._repo.transition(
            current.intent_id,
            target,
            event_type=path_event,
            detail={
                "cum_qty": item.cum_qty,
                "order_qty": item.order_qty,
                "rec_types": sorted({d.RecType for d in item.Details if d.RecType}),
            },
        )
        if target is OrderState.FILLED:
            self._notify_fill(updated, item, partial=False)
        elif item.cum_qty > 0:
            self._notify_fill(updated, item, partial=True)
        return updated

    @staticmethod
    def _terminal_state(item: OrderItem) -> OrderState:
        if item.fully_filled:
            return OrderState.FILLED
        rec_types = {d.RecType for d in item.Details}
        if item.cum_qty > 0 and 6 in rec_types:
            return OrderState.CANCELED  # 一部約定後に残余取消
        if 6 in rec_types:
            return OrderState.CANCELED
        return OrderState.EXPIRED  # 失効・期限切れ・不明終端は失効扱い（手動確認）

    def _request_cancel_remainder(self, intent: Intent, item: OrderItem) -> None:
        if intent.order_id is None:  # pragma: no cover
            return
        already = any(
            r["event_type"] == "CANCEL_REMAINDER_SENT"
            for r in self._repo.events_for(intent.intent_id)
        )
        if already:
            return
        result = self._client.cancelorder(intent.order_id)
        detail = {"reason": repr(result)}
        self._repo.append_event(intent.intent_id, "CANCEL_REMAINDER_SENT", detail)
        if not isinstance(result, ApiSuccess):
            self._notifier.notify(
                Severity.ALERT,
                f"残余取消の要求に失敗: {intent.symbol}",
                str(detail["reason"]),
            )

    def _record_fills(self, intent: Intent, item: OrderItem) -> None:
        for d in item.fills():
            if d.Qty is None or d.Qty <= 0:
                continue
            execution_id = d.ExecutionID or f"{item.ID}-{d.SeqNum}"
            inserted = self._repo.record_execution(
                execution_id=execution_id,
                intent_id=intent.intent_id,
                order_id=item.ID,
                trade_date=intent.trade_date,
                traded_at=d.TransactTime or utc_now_iso(),
                symbol=intent.symbol,
                market=intent.market,
                side=intent.side.value,
                price=d.Price or 0.0,
                qty=d.Qty,
                commission=d.Commission or 0.0,
                commission_tax=d.CommissionTax or 0.0,
            )
            if inserted:
                self._repo.append_event(
                    intent.intent_id,
                    "EXECUTION_RECORDED",
                    {"execution_id": execution_id, "qty": d.Qty, "price": d.Price},
                )

    def _notify_fill(self, intent: Intent, item: OrderItem, *, partial: bool) -> None:
        severity = Severity.ALERT if partial else Severity.INFO
        label = "一部約定で終了（要確認）" if partial else "全約定"
        self._notifier.notify(
            severity,
            f"{label}: {intent.symbol} {intent.side} {item.cum_qty}/{item.order_qty}",
            f"intent={intent.intent_id} order={intent.order_id}",
        )

    def _known_position_symbols(self, trade_date: str) -> set[str]:
        """当日の意図で説明可能な建玉銘柄（非終端または約定終端）."""
        explained: set[str] = set()
        for i in self._repo.list_intents_for_date(trade_date):
            if i.is_open or i.state is OrderState.FILLED or i.state is OrderState.PART_FILLED:
                explained.add(i.symbol)
        return explained

    def _require(self, intent_id: str) -> Intent:
        intent = self._repo.get(intent_id)
        if intent is None:
            raise OrderManagerError(f"意図が存在しません: {intent_id}")
        return intent

    def _note_success(self) -> None:
        if self._kill is not None:
            self._kill.note_success()

    def _note_network_failure(self) -> None:
        if self._kill is None:
            return
        if self._kill.note_network_failure() and self._kill.active:
            # 作動した瞬間のみ通知（連続失敗中は1回）
            reason = self._kill.reasons[-1] if self._kill.reasons else "不明"
            if "通信失敗" in reason:
                self._notifier.notify(
                    Severity.CRITICAL,
                    "キルスイッチ作動: API通信失敗の連続",
                    "以後の発注を停止します。",
                )

    def _note_failure_from(self, result: object) -> None:
        if isinstance(result, HttpFailure | ApiFailure):
            self._note_success()
        else:
            self._note_network_failure()
