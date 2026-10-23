"""⑦ 永続化層（SQLite WAL）.

ブローカーが真実、DBは「意図の台帳」と「突合結果」。
全状態遷移を order_events に追記し監査証跡を残す。
"""

from strata.store.db import connect, current_version, init_db, migrate

__all__ = ["connect", "current_version", "init_db", "migrate"]
