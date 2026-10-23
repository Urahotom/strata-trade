"""意図台帳 (order_intents) とイベントログのリポジトリ.

すべての状態遷移は assert_transition を経由し、同一トランザクションで
order_events に追記する（監査証跡）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from strata.api.models import OrderSide, PriceType
from strata.order.states import OrderState, assert_transition
from strata.store.db import utc_now_iso

OPEN_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.PLANNED,
        OrderState.SENT,
        OrderState.ACK,
        OrderState.WORKING,
        OrderState.PART_FILLED,
    }
)


@dataclass(frozen=True)
class Intent:
    intent_id: str
    trade_date: str  # YYYY-MM-DD
    created_at: str
    updated_at: str
    symbol: str
    market: int
    side: OrderSide
    qty: int
    price_type: PriceType
    #: 指値価格。成行は None（DBではNULL）
    price: float | None
    state: OrderState
    order_id: str | None = None
    reject_code: int | None = None
    reject_message: str | None = None
    request_json: str | None = None
    response_json: str | None = None

    @staticmethod
    def new(
        *,
        trade_date: date | str,
        symbol: str,
        market: int,
        side: OrderSide,
        qty: int,
        price_type: PriceType,
        price: float | None = None,
        request_json: str | None = None,
    ) -> Intent:
        if isinstance(trade_date, date):
            trade_date = trade_date.isoformat()
        now = utc_now_iso()
        return Intent(
            intent_id=str(uuid.uuid4()),
            trade_date=trade_date,
            created_at=now,
            updated_at=now,
            symbol=symbol,
            market=market,
            side=side,
            qty=qty,
            price_type=price_type,
            price=price,
            state=OrderState.PLANNED,
            request_json=request_json,
        )

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


def _row_to_intent(row: sqlite3.Row) -> Intent:
    return Intent(
        intent_id=row["intent_id"],
        trade_date=row["trade_date"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        symbol=row["symbol"],
        market=row["market"],
        side=OrderSide(row["side"]),
        qty=row["qty"],
        price_type=PriceType(row["price_type"]),
        price=row["price"],
        state=OrderState(row["state"]),
        order_id=row["order_id"],
        reject_code=row["reject_code"],
        reject_message=row["reject_message"],
        request_json=row["request_json"],
        response_json=row["response_json"],
    )


class IntentRepository:
    """SQLite意図台帳への唯一の書き込み経路."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---------------- 作成 ----------------

    def create(self, intent: Intent) -> None:
        """PLANNED の意図を作成し INTENT_CREATED イベントを残す."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO order_intents (intent_id, trade_date, created_at,"
                " updated_at, symbol, market, side, qty, price_type, price,"
                " cash_margin, state, request_json) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    intent.intent_id,
                    intent.trade_date,
                    intent.created_at,
                    intent.updated_at,
                    intent.symbol,
                    intent.market,
                    intent.side.value,
                    intent.qty,
                    intent.price_type.value,
                    intent.price,
                    intent.state.value,
                    intent.request_json,
                ),
            )
            self._append_event(intent.intent_id, "INTENT_CREATED", None)

    # ---------------- 状態遷移 ----------------

    def transition(
        self,
        intent_id: str,
        new_state: OrderState,
        *,
        event_type: str,
        order_id: str | None = None,
        reject_code: int | None = None,
        reject_message: str | None = None,
        response_json: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Intent:
        """状態遷移を検証して適用し、イベントを追記する（単一トランザクション）."""
        with self._conn:
            current = self._get_for_update(intent_id)
            assert_transition(current.state, new_state)
            updated_at = utc_now_iso()
            self._conn.execute(
                "UPDATE order_intents SET state = ?, updated_at = ?,"
                " order_id = COALESCE(?, order_id),"
                " reject_code = COALESCE(?, reject_code),"
                " reject_message = COALESCE(?, reject_message),"
                " response_json = COALESCE(?, response_json)"
                " WHERE intent_id = ?",
                (
                    new_state.value,
                    updated_at,
                    order_id,
                    reject_code,
                    reject_message,
                    response_json,
                    intent_id,
                ),
            )
            self._append_event(intent_id, event_type, detail)
            row = self._fetch(intent_id)
        return replace(row, updated_at=updated_at)

    def set_response(self, intent_id: str, response_json: str | None) -> None:
        """送信応答の原文を監査用に保存する（状態は変えない）."""
        with self._conn:
            self._conn.execute(
                "UPDATE order_intents SET response_json = ? WHERE intent_id = ?",
                (response_json, intent_id),
            )

    # ---------------- 読み出し ----------------

    def get(self, intent_id: str) -> Intent | None:
        cur = self._conn.execute("SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,))
        row = cur.fetchone()
        return _row_to_intent(row) if row is not None else None

    def find_active(
        self, *, trade_date: str, symbol: str, side: OrderSide | None = None
    ) -> list[Intent]:
        """非終端状態の意図（同一銘柄・売買方向の重複判定用）."""
        placeholders = ",".join("?" for _ in OPEN_STATES)
        sql = (
            f"SELECT * FROM order_intents WHERE trade_date = ? AND symbol = ?"
            f" AND state IN ({placeholders})"
        )
        params: list[object] = [trade_date, symbol, *[s.value for s in OPEN_STATES]]
        if side is not None:
            sql += " AND side = ?"
            params.append(side.value)
        cur = self._conn.execute(sql, params)
        return [_row_to_intent(r) for r in cur.fetchall()]

    def list_open(self, trade_date: str) -> list[Intent]:
        placeholders = ",".join("?" for _ in OPEN_STATES)
        cur = self._conn.execute(
            f"SELECT * FROM order_intents WHERE trade_date = ?"
            f" AND state IN ({placeholders}) ORDER BY created_at",
            (trade_date, *[s.value for s in OPEN_STATES]),
        )
        return [_row_to_intent(r) for r in cur.fetchall()]

    def list_open_intents(self) -> list[Intent]:
        """日付を問わず非終端意図すべて（翌日約定の突合や横断監視用）."""
        placeholders = ",".join("?" for _ in OPEN_STATES)
        cur = self._conn.execute(
            f"SELECT * FROM order_intents WHERE state IN ({placeholders})"
            " ORDER BY trade_date, created_at",
            tuple(s.value for s in OPEN_STATES),
        )
        return [_row_to_intent(r) for r in cur.fetchall()]

    def claimed_order_ids(self) -> set[str]:
        cur = self._conn.execute("SELECT order_id FROM order_intents WHERE order_id IS NOT NULL")
        return {r["order_id"] for r in cur.fetchall()}

    def count_open_symbols(self, trade_date: str) -> int:
        placeholders = ",".join("?" for _ in OPEN_STATES)
        cur = self._conn.execute(
            f"SELECT COUNT(DISTINCT symbol) AS c FROM order_intents WHERE"
            f" trade_date = ? AND state IN ({placeholders})",
            (trade_date, *[s.value for s in OPEN_STATES]),
        )
        return int(cur.fetchone()["c"])

    def sum_committed_risk(self, trade_date: str) -> float:
        """非終端意図の想定リスク合計の復元用フック.

        意図テーブルは金額を持たないため、評価はエンジンが StopPlan を
        保持して行う。ここではイベント detail の est_risk を合算する。
        """
        placeholders = ",".join("?" for _ in OPEN_STATES)
        cur = self._conn.execute(
            f"SELECT i.intent_id, e.detail FROM order_intents i JOIN"
            f" order_events e ON e.intent_id = i.intent_id WHERE i.trade_date = ?"
            f" AND i.state IN ({placeholders}) AND e.event_type = 'RISK_APPROVED'",
            (trade_date, *[s.value for s in OPEN_STATES]),
        )
        total = 0.0
        seen: set[str] = set()
        for r in cur.fetchall():
            if r["intent_id"] in seen or not r["detail"]:
                continue
            seen.add(r["intent_id"])
            try:
                total += float(json.loads(r["detail"]).get("est_risk", 0.0))
            except (ValueError, TypeError):  # pragma: no cover - 壊れたJSON
                continue
        return total

    # ---------------- 約定 ----------------

    def record_execution(
        self,
        *,
        execution_id: str,
        intent_id: str,
        order_id: str | None,
        trade_date: str,
        traded_at: str,
        symbol: str,
        market: int,
        side: str,
        price: float,
        qty: int,
        commission: float = 0.0,
        commission_tax: float = 0.0,
    ) -> bool:
        """約定を記録する（execution_id 冪等Upsert）。新規記録時True."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO executions (execution_id, intent_id,"
                " order_id, trade_date, traded_at, symbol, market, side, price,"
                " qty, commission, commission_tax) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_id,
                    intent_id,
                    order_id,
                    trade_date,
                    traded_at,
                    symbol,
                    market,
                    side,
                    price,
                    qty,
                    commission,
                    commission_tax,
                ),
            )
            return cur.rowcount > 0

    def executions_for(self, intent_id: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM executions WHERE intent_id = ? ORDER BY traded_at",
            (intent_id,),
        )
        return list(cur.fetchall())

    # ---------------- イベント ----------------

    def append_event(
        self, intent_id: str, event_type: str, detail: dict[str, Any] | None = None
    ) -> None:
        """状態を変えない監査イベントを追記する（RISK_APPROVED等）."""
        with self._conn:
            self._append_event(intent_id, event_type, detail)

    def add_positions_snapshot(self, captured_at: str, holdings: list[dict[str, Any]]) -> int:
        """建玉スナップショットを保存する（起動時リコンサイル結果）."""
        rows = [
            (
                captured_at,
                h["Symbol"],
                int(h.get("Exchange") or 1),
                "BUY" if str(h.get("Side", "2")) != "1" else "SELL",
                int(h.get("HoldQty") or 0),
                h.get("Price"),
                json.dumps(h, ensure_ascii=False),
            )
            for h in holdings
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO positions_snapshot (captured_at, symbol,"
                " market, side, hold_qty, avg_price, raw_json) VALUES (?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def list_positions_snapshot(self, captured_at: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM positions_snapshot WHERE captured_at = ?", (captured_at,)
        )
        return list(cur.fetchall())

    def list_intents_for_date(self, trade_date: str) -> list[Intent]:
        cur = self._conn.execute(
            "SELECT * FROM order_intents WHERE trade_date = ? ORDER BY created_at",
            (trade_date,),
        )
        return [_row_to_intent(r) for r in cur.fetchall()]

    def _append_event(self, intent_id: str, event_type: str, detail: dict[str, Any] | None) -> None:
        self._conn.execute(
            "INSERT INTO order_events (intent_id, occurred_at, event_type, detail)"
            " VALUES (?,?,?,?)",
            (
                intent_id,
                datetime.now().astimezone().isoformat(),
                event_type,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            ),
        )

    def events_for(self, intent_id: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM order_events WHERE intent_id = ? ORDER BY event_id",
            (intent_id,),
        )
        return list(cur.fetchall())

    # ---------------- 内部 ----------------

    def _fetch(self, intent_id: str) -> Intent:
        cur = self._conn.execute("SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,))
        row = cur.fetchone()
        if row is None:  # pragma: no cover - 更新直後の取得、存在前提
            raise LookupError(f"意図が存在しません: {intent_id}")
        return _row_to_intent(row)

    def _get_for_update(self, intent_id: str) -> Intent:
        return self._fetch(intent_id)
