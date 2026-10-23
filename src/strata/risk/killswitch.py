"""キルスイッチ.

発注を停止すべき全局条件（日次損失・通信失敗の連続・オーファン建玉など）で
作動する。作動後は解除まで新規発注を一切拒否する。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class KillSwitch:
    """プロセス内キルスイッチ（日次。再起動時は起動時リコンサイルで再判定）."""

    def __init__(
        self,
        max_daily_loss: float,
        consecutive_failure_threshold: int = 5,
    ) -> None:
        self._max_daily_loss = max_daily_loss
        self._threshold = consecutive_failure_threshold
        self._fail_streak = 0
        self._active = False
        self._reasons: list[str] = []

    @property
    def active(self) -> bool:
        return self._active

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(self._reasons)

    def activate(self, reason: str) -> None:
        """キルスイッチを作動させる（冪等、理由は累積）."""
        if not self._active:
            self._active = True
            logger.critical("キルスイッチ作動: %s", reason)
        elif reason not in self._reasons:
            logger.critical("キルスイッチ追加理由: %s", reason)
        if reason not in self._reasons:
            self._reasons.append(reason)

    def check_daily_pl(self, realized_plus_unrealized: float) -> bool:
        """日次損益（実現+未実現）を受け、閾値超過なら作動して作動状態を返す."""
        if realized_plus_unrealized <= -self._max_daily_loss:
            self.activate(
                f"日次損失 {realized_plus_unrealized:,.0f} "
                f"が上限 -{self._max_daily_loss:,.0f} に到達"
            )
        return self._active

    def note_network_failure(self) -> bool:
        """通信失敗を記録。閾値到達で作動し、作動状態を返す."""
        self._fail_streak += 1
        if self._fail_streak >= self._threshold:
            self.activate(f"API通信失敗が {self._fail_streak} 回連続で発生")
        return self._active

    def note_success(self) -> None:
        """通信成功で連続失敗カウントをリセット."""
        self._fail_streak = 0

    def reset(self) -> None:
        """手動解除（運用ツールからの明示的操作のみ）."""
        self._active = False
        self._reasons = []
        self._fail_streak = 0
        logger.warning("キルスイッチを手動解除しました")
