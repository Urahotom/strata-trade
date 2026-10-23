"""テストスイートの安全装置そのもののテスト.

conftest.py の本番ポート遮断ガードが実際に機能することを検証する。
"""

from __future__ import annotations

import socket

import pytest


def test_本番ポート18080への接続はテスト中に遮断される() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="本番ポート"):
            sock.connect(("127.0.0.1", 18080))
    finally:
        sock.close()


def test_検証ポート18081は遮断されない() -> None:
    # 18081 は「遮断 (AssertionError)」にはならないこと。
    # kabuステーションが動いていなければ ConnectionRefusedError 等で落ちる。
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        try:
            sock.connect(("127.0.0.1", 18081))
        except AssertionError:  # pragma: no cover - ガードが機能していれば発生しない
            pytest.fail("検証ポート18081が遮断されています")
        except OSError:
            pass  # 接続拒否・タイムアウトは許容（ガードの作用ではない）
    finally:
        sock.close()
