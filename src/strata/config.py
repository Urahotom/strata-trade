"""設定の読み込み.

優先順位（高い順）:
    1. 明示的 init 引数
    2. 環境変数（STRATA_ 接頭辞、セクション区切り __）
    3. .env ファイル
    4. TOML設定ファイル（config/config.toml、または STRATA_CONFIG_FILE）

APIパスワード等の秘密情報は TOML に書かず環境変数のみで受け取る。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from strata.errors import ConfigurationError

EnvName = Literal["test", "production"]

#: 検証環境(18081)と本番環境(18080)のポート（取り違え事故を防ぐため一元管理）
PORT_TEST = 18081
PORT_PRODUCTION = 18080

ENV_VAR_CONFIG_FILE = "STRATA_CONFIG_FILE"
DEFAULT_CONFIG_FILE = Path("config/config.toml")


class ApiSettings(BaseModel):
    """kabuステーションAPI接続設定."""

    environment: EnvName = "test"
    host: str = "localhost"
    #: テスト用ポート上書き（通常はNone。検証/本番の決打ちを維持しつつ局所テストを許す）
    port_override: int | None = Field(default=None, ge=1, le=65535)
    #: APIパスワード（環境変数 STRATA_API__PASSWORD のみで設定可能）
    password: SecretStr | None = None
    timeout_connect_s: float = Field(default=2.0, gt=0)
    timeout_read_s: float = Field(default=5.0, gt=0)
    #: 発注系は読み取りタイムアウトを長めに取れる
    timeout_order_read_s: float = Field(default=10.0, gt=0)
    #: 発注カテゴリのレート制限（件/秒）。公式FAQは5件/秒以下、安全側へ
    rate_limit_order_per_s: float = Field(default=3.0, gt=0, le=5)
    #: 情報・余力・登録カテゴリのレート制限（件/秒、上限10件/秒の安全側）
    rate_limit_info_per_s: float = Field(default=8.0, gt=0, le=10)
    #: 歩み値カテゴリのレート制限（件/秒、上限2件/秒の安全側）
    rate_limit_timeandsales_per_s: float = Field(default=1.0, gt=0, le=2)
    #: 取得系のリトライ最大試行回数（発注系は常に1）
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    #: リトライの指数バックオフ基準秒
    retry_backoff_base_s: float = Field(default=0.5, gt=0)

    @property
    def port(self) -> int:
        if self.port_override is not None:
            return self.port_override
        return PORT_PRODUCTION if self.environment == "production" else PORT_TEST

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/kabusapi"

    @property
    def websocket_url(self) -> str:
        return f"ws://{self.host}:{self.port}/kabusapi/websocket"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


class StorageSettings(BaseModel):
    """永続化設定."""

    db_path: Path = Path("var/strata.db")


class RiskSettings(BaseModel):
    """リスク管理設定（P2で本格使用）."""

    #: 1回発注の最大数量（株）
    max_qty_per_order: int = Field(default=1000, gt=0)
    #: 1回発注の最大金額（円）
    max_notional_per_order: float = Field(default=1_000_000, gt=0)
    #: 同時保有ポジション数の上限
    max_open_positions: int = Field(default=10, gt=0)
    #: 1日最大損失（円）。超過でキルスイッチ
    max_daily_loss: float = Field(default=50_000, gt=0)
    #: 日次許容リスク総額（円）。全ポジションの想定ロスカット額の合計上限
    max_total_risk: float = Field(default=100_000, gt=0)


class WatchlistEntry(BaseModel):
    """監視銘柄1件分の設定（エンジン・バックテスト共通）."""

    symbol: str
    #: 単元数量（100株が多いが銘柄依存）
    trading_unit: int = Field(default=100, gt=0)
    #: ロスカット幅（1株あたり）。NoneならRiskSettingsの既定値を使う
    stop_width: float | None = Field(default=None, gt=0)


class SmtpSettings(BaseModel):
    """SMTPメール通知設定."""

    enabled: bool = False
    host: str = "localhost"
    port: int = 587
    use_tls: bool = True
    username: str | None = None
    password: SecretStr | None = None
    from_addr: str | None = None
    to_addr: str | None = None
    timeout_s: float = Field(default=10.0, gt=0)

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("port は 1〜65535")
        return v


class JQuantsSettings(BaseModel):
    """J-Quants API 接続設定."""

    base_url: str = "https://api.jquants.com"
    #: アプリID（シークレット。環境変数 STRATA_DATA__JQUANTS__APP_ID のみ）
    app_id: SecretStr | None = None
    timeout_s: float = Field(default=10.0, gt=0)


class YFinanceSettings(BaseModel):
    """Yahoo Finance (chart API) 接続設定."""

    base_url: str = "https://query1.finance.yahoo.com"
    timeout_s: float = Field(default=10.0, gt=0)


class DataSettings(BaseModel):
    """データ取得設定."""

    provider: Literal["jquants", "yfinance"] = "jquants"
    #: バックテスト開始日（YYYY-MM-DD）
    history_start: str = "2019-01-01"
    #: 当日バーが無いときの挙動: 取引中断（推奨）か警告のみか
    halt_when_today_bar_missing: bool = True
    #: 複数出典の終値突合許容差（相対。例 0.001 = 0.1%）
    close_check_tolerance: float = Field(default=0.001, ge=0, lt=1)
    jquants: JQuantsSettings = Field(default_factory=JQuantsSettings)
    yfinance: YFinanceSettings = Field(default_factory=YFinanceSettings)

    @field_validator("history_start")
    @classmethod
    def _validate_date_format(cls, v: str) -> str:
        from datetime import date

        try:
            date.fromisoformat(v)
        except ValueError as exc:  # pragma: no cover - 検証メッセージ用
            raise ValueError("history_start は YYYY-MM-DD 形式") from exc
        return v


class Settings(BaseSettings):
    """アプリ全体の設定."""

    model_config = SettingsConfigDict(
        env_prefix="STRATA_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    api: ApiSettings = Field(default_factory=ApiSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)
    data: DataSettings = Field(default_factory=DataSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_files = _resolve_toml_files()
        toml_sources: list[PydanticBaseSettingsSource] = [
            TomlConfigSettingsSource(settings_cls, toml_file=p) for p in toml_files
        ]
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            *toml_sources,
            file_secret_settings,
        )

    def require_api_password(self) -> str:
        """APIパスワードを返す。未設定なら ConfigurationError."""
        if self.api.password is None:
            raise ConfigurationError(
                "APIパスワードが未設定です。環境変数 STRATA_API__PASSWORD を設定してください"
            )
        return self.api.password.get_secret_value()


def _resolve_toml_files() -> list[Path]:
    """読み込むTOML設定ファイルの解決（存在するもののみ）."""
    env_specified = os.environ.get(ENV_VAR_CONFIG_FILE)
    if env_specified:
        path = Path(env_specified)
        if not path.is_file():
            raise ConfigurationError(f"設定ファイルが存在しません: {path}")
        return [path]
    if DEFAULT_CONFIG_FILE.is_file():
        return [DEFAULT_CONFIG_FILE]
    return []


def load_settings(config_file: str | os.PathLike[str] | None = None) -> Settings:
    """設定を読み込む.

    Args:
        config_file: 明示的なTOML設定ファイルパス。Noneの場合は
            環境変数 STRATA_CONFIG_FILE → config/config.toml の順で解決。
    """
    if config_file is not None:
        previous = os.environ.get(ENV_VAR_CONFIG_FILE)
        os.environ[ENV_VAR_CONFIG_FILE] = str(config_file)
        try:
            return Settings()
        finally:
            if previous is None:
                os.environ.pop(ENV_VAR_CONFIG_FILE, None)
            else:
                os.environ[ENV_VAR_CONFIG_FILE] = previous
    return Settings()
