"""台帳込みデモ: OrderManager（意図台帳）で発注→約定→台帳記録を一気に見る.

使い方:
    1. ターミナルA: `uv run python -m strata.sim` （フェイクサーバを起動）
    2. ターミナルB: `uv run python scripts/demo_sim.py`

流れ:
    認証 → 買い成行（意図台帳経由）→ フェイクサーバで約定（_sim/finalize）
    → poll で状態同期 → 台帳表示 → 同量を売り戻して終値確認

台帳は var/demo_sim.db（実行のたびに消して再生成）。実発注先が検証環境用の
フェイクサーバ以外になる経路はない（environment=test かつ port_override）。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import httpx
from pydantic import SecretStr

from strata.api.client import KabuClient
from strata.api.models import OrderSide, PriceType, SendOrderRequest
from strata.api.results import ApiSuccess
from strata.config import ApiSettings, Settings, StorageSettings
from strata.notify.base import LoggingNotifier
from strata.order.manager import OrderManager
from strata.order.repository import IntentRepository
from strata.risk.checks import RiskEngine
from strata.risk.killswitch import KillSwitch
from strata.store.db import init_db


def build_settings(args: argparse.Namespace) -> Settings:
    """デモ専用の設定（TOMLに依存せず明示構築。パスワードは環境変数優先）."""
    password = os.environ.get("STRATA_API__PASSWORD", "testpass")
    return Settings(
        api=ApiSettings(
            environment="test",
            host=args.host,
            port_override=args.port,
            password=SecretStr(password),
        ),
        storage=StorageSettings(db_path=Path(args.db)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--symbol", default="8306")
    parser.add_argument("--qty", type=int, default=100)
    parser.add_argument("--price", type=float, default=1000.0)
    parser.add_argument("--db", default="var/demo_sim.db")
    args = parser.parse_args()

    settings = build_settings(args)
    base = f"http://{args.host}:{args.port}/kabusapi"
    today = date.today()

    Path(args.db).unlink(missing_ok=True)
    conn = init_db(settings.storage.db_path)
    notifier = LoggingNotifier()
    client = KabuClient(settings)
    repo = IntentRepository(conn)
    manager = OrderManager(
        repo,
        client,
        notifier,
        risk_engine=RiskEngine(settings.risk),
        kill_switch=KillSwitch(max_daily_loss=settings.risk.max_daily_loss),
    )

    print(f"=== 1. 認証 ({base}) ===")
    auth = client.authenticate()
    if not isinstance(auth, ApiSuccess):
        print(f"認証失敗: {auth}")
        return 1
    print(f"OK: {auth.data}")

    print("\n=== 2. 買い成行を発注（意図台帳経由） ===")
    buy = manager.submit(
        SendOrderRequest(
            symbol=args.symbol, side=OrderSide.BUY, qty=args.qty, price_type=PriceType.MARKET
        ),
        trade_date=today,
        ref_price=args.price,
        stop_width=30.0,
    )
    print(f"intent={buy.intent_id[:8]}... state={buy.state.value} order={buy.order_id}")

    print("\n=== 3. フェイクサーバで約定させる（_sim/finalize） ===")
    order_id = buy.order_id or ""
    resp = httpx.post(
        f"{base}/_sim/finalize",
        json={"order_id": order_id, "qty": args.qty, "price": args.price},
        timeout=5.0,
    )
    resp.raise_for_status()
    print(f"finalize OK: State={resp.json().get('State')}")

    print("\n=== 4. poll で意図台帳を同期 ===")
    manager.poll(today)
    show_intent(repo, buy.intent_id)

    print("\n=== 5. 同量を売り戻す（決済サイクル） ===")
    sell = manager.submit(
        SendOrderRequest(
            symbol=args.symbol, side=OrderSide.SELL, qty=args.qty, price_type=PriceType.MARKET
        ),
        trade_date=today,
        ref_price=args.price,
    )
    resp = httpx.post(
        f"{base}/_sim/finalize",
        json={"order_id": sell.order_id, "qty": args.qty, "price": args.price + 5},
        timeout=5.0,
    )
    resp.raise_for_status()
    manager.poll(today)
    show_intent(repo, sell.intent_id)

    print("\n=== 6. 本日の台帳 ===")
    for intent in repo.list_intents_for_date(today.isoformat()):
        show_intent(repo, intent.intent_id, quiet=True)
    print(f"\n台帳DB: {args.db} / 意図 {len(repo.list_intents_for_date(today.isoformat()))} 件")
    return 0


def show_intent(repo: IntentRepository, intent_id: str, *, quiet: bool = False) -> None:
    intent = repo.get(intent_id)
    if intent is None:  # pragma: no cover - 台帳不整合はあり得ない
        raise SystemExit(f"意図が見つかりません: {intent_id}")
    executions = repo.executions_for(intent_id)
    filled = sum(int(row["qty"] or 0) for row in executions)
    if not quiet:
        print(f"state={intent.state.value} 約定 {filled}/{intent.qty} 株")
    for row in executions:
        print(
            f"  約定記録: {row['side']} {row['qty']}株 @ {row['price']} "
            f"(execution_id={row['execution_id']})"
        )


if __name__ == "__main__":
    sys.exit(main())
