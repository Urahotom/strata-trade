"""ペーパートレードCLIエントリポイント.

    uv run python -m strata.engine run        # 1営業日を通しで実行（当日分）
    uv run python -m strata.engine reconcile  # 認証+起動時リコンサイルだけ実行
    uv run python -m strata.engine status     # 意図台帳の表示（API接続なし）

`run` は act_time まで待機してから発注・close_time まで監視するため、
寄前の開始でよい（タスクスケジューラで毎営業日 08:45 起動などを想定）。
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from datetime import date

from strata.config import Settings, load_settings
from strata.engine.day import DayPhase
from strata.engine.service import PaperTrader, Runtime, build_runtime
from strata.errors import ConfigurationError
from strata.order.repository import IntentRepository
from strata.store.db import init_db

logger = logging.getLogger("strata.engine")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    trade_date = date.fromisoformat(args.date) if args.date else date.today()
    try:
        settings = load_settings(args.config)
        if args.command == "status":
            return _status(settings, trade_date)
        runtime = build_runtime(settings, require_provider=args.command == "run")
        trader = PaperTrader(runtime)
        if args.command == "run":
            return _run(runtime, trader, trade_date)
        return _reconcile(runtime, trader)
    except ConfigurationError as exc:
        logger.error("設定エラー: %s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - 手動停止時の整理
        logger.warning("中断されました")
        return 130


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m strata.engine",
        description="1営業日エンジン（ペーパートレード）",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="TOML設定ファイル（既定 config/config.toml）")
    common.add_argument("--date", help="対象営業日 YYYY-MM-DD（既定は本日）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", parents=[common], help="1営業日を実行（待機→発注→監視→終日処理）")
    sub.add_parser("reconcile", parents=[common], help="認証+起動時リコンサイルのみ")
    sub.add_parser("status", parents=[common], help="意図台帳の表示（API接続なし）")
    return parser.parse_args(argv)


def _run(runtime: Runtime, trader: PaperTrader, trade_date: date) -> int:
    if not runtime.settings.watchlist:
        logger.warning("watchlist が未設定です。発注は何も発生しません")
    phase = trader.run(trade_date)
    engine = runtime.engine
    if engine.halted_reason:
        logger.error("取引中断: %s", engine.halted_reason)
    print(f"1営業日終了: phase={phase.value} 中断={'はい' if engine.halted_reason else 'いいえ'}")
    ok = phase is DayPhase.FINISHED and engine.halted_reason is None
    return 0 if ok else 1


def _reconcile(runtime: Runtime, trader: PaperTrader) -> int:
    report = trader.bootstrap()
    if report is None:
        print("リコンサイル失敗（認証失敗またはキルスイッチ作動）。ログと通知を確認してください")
        return 1
    print(f"=== 起動時リコンサイル報告 {date.today()} ===")
    print(f"未了意図: {len(report.open_intents)} 件")
    for intent in report.open_intents:
        print(f"  {intent.intent_id} {intent.state.value} {intent.symbol} {intent.side.value}")
    print(f"建玉スナップショット: {report.snapshot_captured_at or '取得失敗'}")
    print(f"オーファン建玉: {len(report.orphan_positions)} 件")
    return 1 if report.orphan_positions else 0


def _status(settings: Settings, trade_date: date) -> int:
    conn = init_db(settings.storage.db_path)
    repo = IntentRepository(conn)
    intents = repo.list_intents_for_date(trade_date.isoformat())
    print(f"=== 意図台帳 {trade_date}（{len(intents)} 件） ===")
    for intent in intents:
        filled = sum(int(row["qty"] or 0) for row in repo.executions_for(intent.intent_id))
        note = intent.reject_message or ""
        print(
            f"{intent.intent_id} {intent.state.value:<9} {intent.symbol} "
            f"{intent.side.value} {intent.qty}株 約定{filled}株 "
            f"order={intent.order_id or '-'} {note}".rstrip()
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
