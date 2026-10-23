"""strata.store（SQLite マイグレーション）のユニットテスト."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from strata.errors import MigrationError, StoreError
from strata.store import connect, current_version, init_db, migrate
from strata.store.db import check_schema

EXPECTED_TABLES = {
    "order_intents",
    "order_events",
    "executions",
    "positions_snapshot",
    "bars_daily",
    "symbols",
    "schema_version",
}


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_初期化ですべてのテーブルを作成する(db: sqlite3.Connection) -> None:
    assert EXPECTED_TABLES.issubset(_tables(db))


def test_WALと外部キーが有効(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_マイグレーションは何回実行しても冪等(tmp_path: Path) -> None:
    db_path = tmp_path / "re.db"
    conn = connect(db_path)
    assert migrate(conn) == [1]
    assert migrate(conn) == []
    assert migrate(conn) == []
    assert current_version(conn) == 1
    conn.close()


def test_適用済みバージョンを記録する(db: sqlite3.Connection) -> None:
    row = db.execute("SELECT version, name FROM schema_version").fetchone()
    assert row["version"] == 1
    assert row["name"] == "0001_init.sql"


def test_未初期化DBのチェックはエラー(tmp_path: Path) -> None:
    conn = connect(tmp_path / "empty.db")
    with pytest.raises(StoreError):
        check_schema(conn)
    conn.close()


def test_不正なファイル名のマイグレーションはエラー(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = connect(tmp_path / "x.db")
    with pytest.raises(MigrationError):
        migrate(conn, mig_dir)
    conn.close()


def test_バージョン重複のマイグレーションはエラー(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_a.sql").write_text("CREATE TABLE t1 (x INT);", encoding="utf-8")
    (mig_dir / "0001_b.sql").write_text("CREATE TABLE t2 (x INT);", encoding="utf-8")
    conn = connect(tmp_path / "x.db")
    with pytest.raises(MigrationError):
        migrate(conn, mig_dir)
    conn.close()


INTENT_SQL = """
INSERT INTO order_intents
    (intent_id, trade_date, created_at, updated_at, symbol, market, side,
     qty, price_type, price, state)
VALUES
    (:intent_id, '2026-10-23', '2026-10-23T08:00:00+09:00',
     '2026-10-23T08:00:00+09:00', '8306', 1, :side, :qty, :price_type,
     :price, 'PLANNED')
"""


def test_intent挿入と読み出しのラウンドトリップ(db: sqlite3.Connection) -> None:
    db.execute(
        INTENT_SQL,
        {
            "intent_id": "intent-1",
            "side": "BUY",
            "qty": 100,
            "price_type": "MARKET",
            "price": None,
        },
    )
    db.commit()
    row = db.execute("SELECT * FROM order_intents WHERE intent_id = 'intent-1'").fetchone()
    assert row["state"] == "PLANNED"
    assert row["cash_margin"] == 1  # デフォルト現物
    assert row["price"] is None


def test_数量0はCHECK制約で拒否(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            INTENT_SQL,
            {
                "intent_id": "intent-bad",
                "side": "BUY",
                "qty": 0,
                "price_type": "MARKET",
                "price": None,
            },
        )


def test_売買区分の列挙外は拒否(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            INTENT_SQL,
            {
                "intent_id": "intent-bad2",
                "side": "HOLD",
                "qty": 100,
                "price_type": "MARKET",
                "price": None,
            },
        )


def test_信用注文はP0では拒否(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO order_intents (intent_id, trade_date, created_at,"
            " updated_at, symbol, market, side, qty, price_type, cash_margin,"
            " state) VALUES ('i3','2026-10-23','t','t','8306',1,'BUY',100,"
            "'MARKET', 2, 'PLANNED')"
        )


def test_order_idの一意制約_partial_unique(db: sqlite3.Connection) -> None:
    base = {
        "trade_date": "2026-10-23",
        "created_at": "t",
        "updated_at": "t",
        "symbol": "8306",
        "market": 1,
        "side": "BUY",
        "qty": 100,
        "price_type": "MARKET",
        "state": "ACK",
    }
    db.execute(
        "INSERT INTO order_intents (intent_id, order_id, trade_date, created_at,"
        " updated_at, symbol, market, side, qty, price_type, state)"
        " VALUES (:iid, 'OID-1', :trade_date, :created_at, :updated_at, :symbol,"
        " :market, :side, :qty, :price_type, :state)",
        {"iid": "a", **base},
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO order_intents (intent_id, order_id, trade_date,"
            " created_at, updated_at, symbol, market, side, qty, price_type,"
            " state) VALUES (:iid, 'OID-1', :trade_date, :created_at,"
            " :updated_at, :symbol, :market, :side, :qty, :price_type, :state)",
            {"iid": "b", **base},
        )
    # order_id は NULL 複数許容（未送信意図の並行保持）
    db.execute(
        "INSERT INTO order_intents (intent_id, trade_date, created_at, updated_at,"
        " symbol, market, side, qty, price_type, state)"
        " VALUES ('c', :trade_date, :created_at, :updated_at, :symbol, :market,"
        " :side, :qty, :price_type, 'PLANNED')",
        base,
    )
    db.execute(
        "INSERT INTO order_intents (intent_id, trade_date, created_at, updated_at,"
        " symbol, market, side, qty, price_type, state)"
        " VALUES ('d', :trade_date, :created_at, :updated_at, :symbol, :market,"
        " :side, :qty, :price_type, 'PLANNED')",
        base,
    )


def test_order_eventsは意図への外部キー必須(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO order_events (intent_id, occurred_at, event_type)"
            " VALUES ('not-exists', 't', 'CREATED')"
        )


def test_bars_dailyは出典別で同一日付共存可(db: sqlite3.Connection) -> None:
    for source in ("jquants", "yfinance"):
        db.execute(
            "INSERT INTO bars_daily (symbol, date, open, high, low, close,"
            " volume, source) VALUES ('8306', '2026-10-22', 1, 2, 0.5, 1.5,"
            " 1000, ?)",
            (source,),
        )
    n = db.execute("SELECT COUNT(*) AS c FROM bars_daily").fetchone()["c"]
    assert n == 2


def test_bars_dailyの高安逆行は拒否(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO bars_daily (symbol, date, open, high, low, close,"
            " volume, source) VALUES ('8306', '2026-10-22', 1, 1, 5, 1.5,"
            " 1000, 'manual')"
        )
