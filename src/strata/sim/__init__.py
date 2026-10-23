"""⑩ フェイクkabuステーションサーバ（開発・テスト専用）.

実kabuステーションの仕様（トークン失効・認証・発注Result・注文状態）を
模したFastAPIアプリ。シナリオ注入で障害ケースを再現する。
本番コードから決してimportしないこと（tests/ とツールからのみ使用する）。
"""

from strata.sim.server import SimBroker, create_app

__all__ = ["SimBroker", "create_app"]
