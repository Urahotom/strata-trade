"""リスク管理層.

発注前チェック、ポジションサイジング、キルスイッチ。
戦略層から直接参照されることはなく、エンジン/注文管理経由でのみ呼ばれる。
"""

from strata.risk.checks import RiskContext, RiskEngine, RiskVerdict, SubmitSpec
from strata.risk.killswitch import KillSwitch

__all__ = [
    "KillSwitch",
    "RiskContext",
    "RiskEngine",
    "RiskVerdict",
    "SubmitSpec",
]
