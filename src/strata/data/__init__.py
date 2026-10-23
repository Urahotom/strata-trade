"""② データ層: 日足プロバイダ抽象・アダプタ・品質バリデーション・永続化."""

from __future__ import annotations

from strata.config import DataSettings
from strata.data.base import (
    DailyBar,
    DataProviderError,
    MarketDataProvider,
    to_code,
    to_dot_symbol,
)
from strata.data.jquants import JQuantsProvider
from strata.data.store import BarRepository, CloseMismatch
from strata.data.validate import QualityReport, RejectedBar, jst_today, validate_bars
from strata.data.yfinance import YFinanceProvider
from strata.errors import ConfigurationError

__all__ = [
    "BarRepository",
    "CloseMismatch",
    "DailyBar",
    "DataProviderError",
    "JQuantsProvider",
    "MarketDataProvider",
    "QualityReport",
    "RejectedBar",
    "YFinanceProvider",
    "jst_today",
    "make_provider",
    "to_code",
    "to_dot_symbol",
    "validate_bars",
]


def make_provider(settings: DataSettings) -> MarketDataProvider:
    """設定から日足プロバイダを生成する（シークレット未設定は設定エラー）."""
    if settings.provider == "jquants":
        app_id = settings.jquants.app_id
        if app_id is None:
            raise ConfigurationError(
                "J-Quants APIキーが未設定です。環境変数 "
                "STRATA_DATA__JQUANTS__APP_ID を設定してください"
            )
        return JQuantsProvider(
            app_id.get_secret_value(),
            base_url=settings.jquants.base_url,
            timeout_s=settings.jquants.timeout_s,
        )
    return YFinanceProvider(
        base_url=settings.yfinance.base_url,
        timeout_s=settings.yfinance.timeout_s,
    )
