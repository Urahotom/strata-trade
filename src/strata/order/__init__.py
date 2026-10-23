"""⑤ 注文管理層.

Intent Log（Write-Ahead）に基づく注文ライフサイクル管理と起動時リコンサイル。
"""

from strata.order.manager import (
    CrossOrderError,
    DuplicateIntentError,
    KillSwitchActiveError,
    OrderManager,
    OrderManagerError,
    PartialFillPolicy,
    RiskBlockedError,
    StartupReport,
)
from strata.order.repository import Intent, IntentRepository
from strata.order.states import (
    TERMINAL_STATES,
    OrderState,
    assert_transition,
)

__all__ = [
    "TERMINAL_STATES",
    "CrossOrderError",
    "DuplicateIntentError",
    "Intent",
    "IntentRepository",
    "KillSwitchActiveError",
    "OrderManager",
    "OrderManagerError",
    "OrderState",
    "PartialFillPolicy",
    "RiskBlockedError",
    "StartupReport",
    "assert_transition",
]
