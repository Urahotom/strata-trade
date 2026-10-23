"""strata.config のユニットテスト（オフライン）."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from strata.config import Settings, load_settings
from strata.errors import ConfigurationError

TOML_SAMPLE = """
[api]
environment = "production"
host = "192.168.1.50"

[storage]
db_path = "data/test.db"

[risk]
max_qty_per_order = 500
max_daily_loss = 10000

[smtp]
enabled = true
host = "smtp.example.com"
port = 465
use_tls = false

[data]
provider = "yfinance"
"""


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os_environ_keys()):
        if key.startswith("STRATA_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("STRATA_CONFIG_FILE", raising=False)
    monkeypatch.chdir(Path(__file__).parent)  # config/config.toml を読ませない


def os_environ_keys() -> list[str]:
    import os

    return list(os.environ.keys())


def test_デフォルト設定は検証環境(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    s = load_settings()
    assert s.api.environment == "test"
    assert s.api.port == 18081
    assert s.api.base_url == "http://localhost:18081/kabusapi"
    assert s.api.websocket_url == "ws://localhost:18081/kabusapi/websocket"
    assert not s.api.is_production


def test_TOML設定の読み込み(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text(TOML_SAMPLE, encoding="utf-8")
    s = load_settings(cfg)
    assert s.api.environment == "production"
    assert s.api.port == 18080
    assert s.api.base_url == "http://192.168.1.50:18080/kabusapi"
    assert s.storage.db_path == Path("data/test.db")
    assert s.risk.max_qty_per_order == 500
    assert s.risk.max_daily_loss == 10000
    assert s.smtp.enabled is True
    assert s.data.provider == "yfinance"


def test_環境変数はTOMLより優先(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text(TOML_SAMPLE, encoding="utf-8")
    monkeypatch.setenv("STRATA_API__ENVIRONMENT", "test")
    s = load_settings(cfg)
    assert s.api.environment == "test"
    assert s.api.port == 18081


def test_パスワードは環境変数のみ(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    s = load_settings()
    with pytest.raises(ConfigurationError):
        s.require_api_password()
    monkeypatch.setenv("STRATA_API__PASSWORD", "secret123")
    s2 = load_settings()
    assert s2.require_api_password() == "secret123"
    # シークレットなのでログには出さない
    assert "secret123" not in repr(s2)


def test_不正な環境名は拒否(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("STRATA_API__ENVIRONMENT", "staging")
    with pytest.raises(ValidationError):
        load_settings()


def test_TOMLの未知のセクションは拒否(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clean_env(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[unknown_section]\nfoo = 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(cfg)


def test_存在しない設定ファイルはエラー(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    with pytest.raises(ConfigurationError):
        load_settings("does/not/exist.toml")


def test_レート制限は公式上限を超えられない(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_env(monkeypatch)
    monkeypatch.setenv("STRATA_API__RATE_LIMIT_ORDER_PER_S", "100")
    with pytest.raises(ValidationError):
        load_settings()


def test_Settings直接構築でベースURL組み立て() -> None:
    s = Settings(api={"environment": "production", "host": "10.0.0.9"})
    assert s.api.base_url == "http://10.0.0.9:18080/kabusapi"
