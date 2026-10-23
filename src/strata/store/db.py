"""SQLite 接続とスキーママイグレーション.

- WAL モード・外部キー制約を有効にした接続を提供する
- マイグレーションは src/strata/store/migrations/*.sql を番号順に1回ずつ適用する
- 適用済みバージョンは schema_version テーブルで管理する
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from strata.errors import MigrationError, StoreError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_MIGRATION_NAME_RE = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def utc_now_iso() -> str:
    """UTCのISO8601タイムスタンプ（マイクロ秒付き）を返す."""
    return datetime.now(UTC).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    """運用用接続を開く（WAL・外部キー有効・Rowファクトリ）.

    親ディレクトリがなければ作成する。`:memory:` は初期化専用。
    """
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
    else:  # pragma: no cover - テスト用
        conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA_VERSION_DDL)
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    """適用済みの最新スキーマバージョンを返す（未適用は 0）."""
    _ensure_schema_version(conn)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row is not None and row["v"] is not None else 0


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    found: list[tuple[int, str, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_NAME_RE.match(path.name)
        if m is None:
            raise MigrationError(
                f"マイグレーションファイル名が不正です: {path.name} (NNNN_name.sql 形式)"
            )
        found.append((int(m.group("version")), path.name, path))
    versions = [v for v, _, _ in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"マイグレーションバージョンが重複しています: {versions}")
    return found


def migrate(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[int]:
    """未適用マイグレーションを番号順に適用し、適用したバージョン一覧を返す.

    各ファイルは単一トランザクションで適用され、途中失敗時はロールバックされる。
    """
    _ensure_schema_version(conn)
    applied: list[int] = []
    version = current_version(conn)
    for mig_version, name, path in _discover_migrations(migrations_dir):
        if mig_version <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute("BEGIN")
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                (mig_version, name, utc_now_iso()),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise MigrationError(f"マイグレーション {name} の適用に失敗: {exc}") from exc
        applied.append(mig_version)
        version = mig_version
    return applied


def init_db(db_path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> sqlite3.Connection:
    """DB初期化（接続作成→マイグレーション適用済み接続を返す）."""
    conn = connect(db_path)
    try:
        migrate(conn, migrations_dir)
    except Exception:
        conn.close()
        raise
    return conn


def check_schema(conn: sqlite3.Connection) -> None:
    """スキーマが期待通りかの簡単な健全性チェック（失敗で StoreError）."""
    try:
        conn.execute("SELECT 1 FROM order_intents LIMIT 0")
    except sqlite3.Error as exc:
        raise StoreError(f"スキーマが壊れています（未初期化の可能性）: {exc}") from exc
