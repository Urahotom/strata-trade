"""strata-trade 共通例外階層.

API層の「返り値で返すエラー」とは別に、設定・永続化など
アプリ側で例外として扱う系統をここに定義する。
"""

from __future__ import annotations


class StrataError(Exception):
    """strata-trade の基底例外."""


class ConfigurationError(StrataError):
    """設定の読み込み・検証に失敗した場合の例外."""


class StoreError(StrataError):
    """永続化層（SQLite）のエラー."""


class MigrationError(StoreError):
    """スキーママイグレーションの失敗."""
