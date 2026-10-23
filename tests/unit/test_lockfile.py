"""トークン発行ロック（IssuanceLock）のテスト."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from strata.api.lockfile import IssuanceLock, LockHeldError


def test_取得と解放でファイルが消える(tmp_path: Path) -> None:
    lock_path = tmp_path / "token.lock"
    with IssuanceLock(lock_path):
        assert lock_path.exists()
        assert lock_path.read_text(encoding="ascii") == str(os.getpid())
    assert not lock_path.exists()


def test_保持中は他が待ってタイムアウト(tmp_path: Path) -> None:
    lock_path = tmp_path / "token.lock"
    with IssuanceLock(lock_path), pytest.raises(LockHeldError):
        IssuanceLock(lock_path, timeout_s=0.1, stale_s=30.0).acquire()


def test_staleな残骸は自動除去(tmp_path: Path) -> None:
    lock_path = tmp_path / "token.lock"
    lock_path.write_text("999999", encoding="ascii")
    old = time.time() - 3600
    os.utime(lock_path, (old, old))
    lock = IssuanceLock(lock_path, timeout_s=0.5, stale_s=30.0)
    lock.acquire()  # 残骸を除去して取得できる
    assert lock_path.exists()
    lock.release()


def test_親ディレクトリがなくても作成(tmp_path: Path) -> None:
    lock_path = tmp_path / "deep" / "nested" / "token.lock"
    with IssuanceLock(lock_path):
        assert lock_path.exists()
