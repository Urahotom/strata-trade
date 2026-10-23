"""フェイクkabuステーションの単独起動.

    uv run python -m strata.sim                 # 既定: 127.0.0.1:18081 / パスワード testpass
    uv run python -m strata.sim --port 18090 --password s3cret

起動したら別の端末でクライアントをこのサーバへ向ける（検証環境既定の
18081 にした場合はそのまま `api.environment="test"` の設定で繋がる）:

    $env:STRATA_API__PASSWORD = "testpass"
    # ポートを変える場合のみ:
    $env:STRATA_API__PORT_OVERRIDE = "18090"

手動でシナリオを注入するための制御API（公式仕様外・認証不要）を用意:
    GET  /kabusapi/_sim/state                 現在の状態ダンプ
    POST /kabusapi/_sim/reject   {"code":21}  次発注をリジェクト化
    POST /kabusapi/_sim/drop_next             次発注の応答を喪失
    POST /kabusapi/_sim/delay    {"seconds":2} 全API遅延
    POST /kabusapi/_sim/finalize {"order_id":..,"qty":..,"mode":"expire|cancel","price":..}
    POST /kabusapi/_sim/holdings {"holdings":[...]}   /positions の建玉設定
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

from strata.sim.server import SimBroker, create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18081
DEFAULT_PASSWORD = "testpass"


def build(host: str, port: int, password: str) -> tuple[SimBroker, FastAPI]:
    """起動する (broker, app) を組み立てる（テストから呼ぶための分離点）."""
    broker = SimBroker(api_password=password)
    return broker, create_app(broker)


def _banner(host: str, port: int, password: str) -> None:
    override = "" if port == DEFAULT_PORT else f'\n  $env:STRATA_API__PORT_OVERRIDE = "{port}"'
    print("=" * 64)
    print("フェイクkabuステーションAPI (strata.sim) 起動")
    print("=" * 64)
    print(f"ベースURL : http://{host}:{port}/kabusapi")
    print(f"APIパスワード: {password}")
    print("- 終了は Ctrl+C")
    print("- クライアント側の設定:")
    print(
        f'  $env:STRATA_API__PASSWORD = "{password}"'
        f"   # 検証環境既定ポート {DEFAULT_PORT} なら他不要"
    )
    if override:
        print(override)
    print("- 制御API (開発用):")
    print(f"  curl http://{host}:{port}/kabusapi/_sim/state")
    print("=" * 64)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m strata.sim",
        description="フェイクkabuステーションAPIサーバを開発・テスト用に起動する",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="バインドホスト（既定 127.0.0.1）")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"ポート（既定 {DEFAULT_PORT}=検証環境と同じ。変えると client 側 port_override 必須）",
    )
    parser.add_argument(
        "--password", default=DEFAULT_PASSWORD, help="APIパスワード（既定 testpass）"
    )
    parser.add_argument("--no-banner", action="store_true", help="起動バナーを出さない")
    args = parser.parse_args(argv)

    import uvicorn

    _, app = build(args.host, args.port, args.password)
    if not args.no_banner:
        _banner(args.host, args.port, args.password)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
