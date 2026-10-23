"""通知の抽象インターフェース.

取引パス（発注・約定・障害検知）から呼ばれるため、実装は例外を投げない。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import IntEnum
from typing import Protocol


class Severity(IntEnum):
    INFO = 0
    ALERT = 1
    CRITICAL = 2


class Notifier(Protocol):
    """通知先プロトコル。実装は例外を送出してはならない."""

    def notify(self, severity: Severity, subject: str, body: str = "") -> None: ...


class InMemoryNotifier:
    """テスト・開発用。メモリに保持する."""

    class Record:
        def __init__(self, severity: Severity, subject: str, body: str) -> None:
            self.severity = severity
            self.subject = subject
            self.body = body

        def __repr__(self) -> str:  # pragma: no cover - デバッグ用
            return f"Record({self.severity.name}, {self.subject!r})"

    def __init__(self) -> None:
        self.records: list[InMemoryNotifier.Record] = []

    def notify(self, severity: Severity, subject: str, body: str = "") -> None:
        self.records.append(self.Record(severity, subject, body))

    def subjects(self, severity: Severity | None = None) -> Sequence[str]:
        return [r.subject for r in self.records if severity is None or r.severity == severity]


class LoggingNotifier:
    """ログのみへ出力する通知（INFO/ERROR/CRITICAL対応）."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("strata.notify")

    def notify(self, severity: Severity, subject: str, body: str = "") -> None:
        level = {
            Severity.INFO: logging.INFO,
            Severity.ALERT: logging.ERROR,
            Severity.CRITICAL: logging.CRITICAL,
        }[severity]
        message = f"[{severity.name}] {subject}"
        if body:
            message = f"{message}\n{body}"
        self._logger.log(level, message)
