"""テスト全体共通のフィクスチャ.

【安全ガード】
- 本番ポート (18080) へのソケット接続をテストプロセス全体で遮断する。
  遮断されたらテスト失敗（AssertionError）になる。
- live マーカーのテストは STRATA_ALLOW_LIVE=1 のときだけ実行され、
  その場合も 18080 への接続は遮断したままとする（検証環境 18081 のみ許可）。
"""

from __future__ import annotations

import os
import socket

import pytest

#: テスト中に絶対に接続してはならないポート（本番 kabuステーション）
FORBIDDEN_PORTS = frozenset({18080})
#: live テストが STRATA_ALLOW_LIVE=1 で追加接続を許可されるポート（検証環境）
LIVE_ALLOWED_PORT = 18081

_real_connect = socket.socket.connect


def _is_forbidden(address: object) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    port = address[1]
    return isinstance(port, int) and port in FORBIDDEN_PORTS


@pytest.fixture(autouse=True)
def clean_strata_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """開発機の STRATA_* 環境変数がテストに漏れないようにする（AllowLive は残す）."""
    for key in list(os.environ):
        if key.startswith("STRATA_") and key != "STRATA_ALLOW_LIVE":
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def production_port_guard(request: pytest.FixtureRequest) -> None:
    """本番ポートへの接続を遮断する autouse フィクスチャ."""
    is_live = request.node.get_closest_marker("live") is not None
    if is_live and os.environ.get("STRATA_ALLOW_LIVE") != "1":
        pytest.skip("live テストは STRATA_ALLOW_LIVE=1 のときのみ実行できます")

    def guarded_connect(self: socket.socket, address: object) -> None:
        if _is_forbidden(address):
            host = address[0] if isinstance(address, tuple) else address
            raise AssertionError(
                f"テスト中の本番ポート(18080)への接続を試みました: {host}。"
                "テストはオフラインまたは検証環境のみで実行してください。"
            )
        _real_connect(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    try:
        yield None  # type: ignore[misc]
    finally:
        socket.socket.connect = _real_connect  # type: ignore[method-assign]
