"""通知層のテスト（SMTPは送信関数を注入しネットワークゼロ）."""

from __future__ import annotations

import logging
from email.message import EmailMessage

import pytest

from strata.config import SmtpSettings
from strata.notify.base import InMemoryNotifier, LoggingNotifier, Severity
from strata.notify.mailer import MailNotifier


class TestInMemoryNotifier:
    def test_記録とフィルタ(self) -> None:
        n = InMemoryNotifier()
        n.notify(Severity.INFO, "info件名")
        n.notify(Severity.CRITICAL, "crit件名", "本文")
        assert n.subjects() == ["info件名", "crit件名"]
        assert n.subjects(Severity.CRITICAL) == ["crit件名"]
        assert n.records[1].body == "本文"


class TestLoggingNotifier:
    def test_重大度対応のログレベル(self, caplog: pytest.LogCaptureFixture) -> None:
        notifier = LoggingNotifier(logging.getLogger("strata.test.notify"))
        with caplog.at_level(logging.INFO, logger="strata.test.notify"):
            notifier.notify(Severity.INFO, "軽い通知")
            notifier.notify(Severity.CRITICAL, "重大")
        levels = [r.levelno for r in caplog.records]
        assert levels == [logging.INFO, logging.CRITICAL]
        assert "重大" in caplog.text


class TestMailNotifier:
    def settings(self, **kw: object) -> SmtpSettings:
        base: dict[str, object] = {
            "enabled": True,
            "from_addr": "from@example.com",
            "to_addr": "to@example.com",
        }
        base.update(kw)
        return SmtpSettings(**base)  # type: ignore[arg-type]

    def test_有効時は注入したsend_fnを呼ぶ(self) -> None:
        sent: list[EmailMessage] = []
        notifier = MailNotifier(self.settings(), send_fn=sent.append)
        notifier.notify(Severity.ALERT, "約定", "詳細本文")
        assert len(sent) == 1
        msg = sent[0]
        assert msg["Subject"] == "[STRATA:ALERT] 約定"
        assert msg["To"] == "to@example.com"
        assert msg.get_content().strip() == "詳細本文"

    def test_無効時はsend_fnを呼ばずfailoverへ(self) -> None:
        sent: list[EmailMessage] = []
        fallback = InMemoryNotifier()
        notifier = MailNotifier(
            self.settings(enabled=False), send_fn=sent.append, failover=fallback
        )
        notifier.notify(Severity.INFO, "送らない")
        assert sent == []
        assert fallback.subjects() == ["送らない"]

    def test_送信失敗は握ってログのみ(self, caplog: pytest.LogCaptureFixture) -> None:
        def boom(msg: EmailMessage) -> None:
            raise OSError("smtp down")

        notifier = MailNotifier(self.settings(), send_fn=boom)
        with caplog.at_level(logging.ERROR):
            notifier.notify(Severity.CRITICAL, "いける？")  # 例外を投げない
        assert "メール通知に失敗" in caplog.text

    def test_本文なしは件名を流用(self) -> None:
        sent: list[EmailMessage] = []
        notifier = MailNotifier(self.settings(), send_fn=sent.append)
        notifier.notify(Severity.INFO, "件名のみ")
        assert sent[0].get_content().strip() == "件名のみ"
