"""SMTPメール通知.

smtplib を使うが、send_fn を注入可能にしてテストでネットワークゼロにする。
いかなる失敗も例外にしない（取引ループを止めない）。失敗はログと返り値で示す。
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from strata.config import SmtpSettings
from strata.notify.base import Severity

logger = logging.getLogger(__name__)

#: (msg) -> None の送信関数。失敗時は例外を投げてよい（ここで吸収する）
SendFn = Callable[[EmailMessage], None]


class MailNotifier:
    """設定されたSMTPサーバーでメールを送るNotifier実装."""

    def __init__(
        self,
        settings: SmtpSettings,
        *,
        send_fn: SendFn | None = None,
        failover: object | None = None,
    ) -> None:
        self._settings = settings
        self._send_fn = send_fn or self._send_with_smtp
        self._failover = failover

    def notify(self, severity: Severity, subject: str, body: str = "") -> None:
        if not self._settings.enabled:
            if self._failover is not None:
                assert hasattr(self._failover, "notify")
                self._failover.notify(severity, subject, body)
            return
        msg = EmailMessage()
        msg["Subject"] = f"[STRATA:{severity.name}] {subject}"
        msg["From"] = self._settings.from_addr or "strata@localhost"
        msg["To"] = self._settings.to_addr or ""
        msg.set_content(body or subject)
        try:
            self._send_fn(msg)
        except Exception as exc:  # 通知失敗で取引を止めない
            logger.error("メール通知に失敗しました (%s): %s", subject, exc)

    def _send_with_smtp(self, msg: EmailMessage) -> None:
        s = self._settings
        if not s.to_addr:
            raise ValueError("smtp.to_addr が未設定")
        with smtplib.SMTP(s.host, s.port, timeout=s.timeout_s) as smtp:
            if s.use_tls:
                smtp.starttls()
            if s.username and s.password:
                smtp.login(s.username, s.password.get_secret_value())
            smtp.send_message(msg)
