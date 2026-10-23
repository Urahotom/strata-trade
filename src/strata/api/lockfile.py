"""トークン発行の直列化用ファイルロック.

同一マシンで複数プロセスが同時に POST /token すると、後に発行された
トークンで前者が失効してしまう（公式仕様: 別トークン発行で旧失効）。
発行操作だけを短い排他ロックで直列化する。
"""

from __future__ import annotations

import contextlib
import errno
import os
import time
from pathlib import Path

_STALE_SECONDS = 30.0


class LockHeldError(TimeoutError):
    """他のプロセスがロックを保持し続けている."""


class IssuanceLock:
    """O_EXCL ベースの簡易ファイルロック（cross-platform）.

    使用法:
        with IssuanceLock(Path("var/token.lock")):
            ...  # トークン発行
    """

    def __init__(
        self,
        path: Path,
        timeout_s: float = 10.0,
        stale_s: float = _STALE_SECONDS,
        poll_interval_s: float = 0.05,
    ) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._stale_s = stale_s
        self._poll = poll_interval_s
        self._acquired = False

    def _try_create(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True

    def _is_stale(self) -> bool:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return False
        return (time.time() - mtime) > self._stale_s

    def acquire(self) -> None:
        deadline = time.monotonic() + self._timeout_s
        while True:
            if self._try_create():
                self._acquired = True
                return
            if self._is_stale():
                # 保持プロセスのクラッシュ残骸と判断して削除する
                with contextlib.suppress(OSError):  # pragma: no cover - 競合時のみ
                    self._path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise LockHeldError(f"トークン発行ロックの取得を超时しました: {self._path}")
            time.sleep(self._poll)

    def release(self) -> None:
        if self._acquired:
            self._path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> IssuanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
