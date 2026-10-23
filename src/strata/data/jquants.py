"""J-Quants 株式日次スナップショット アダプタ.

公式v1エンドポイント準拠:
    POST /v1/auth/tokens/login   {"appid": ...}            -> {"JWT": ...}
    POST /v1/topics/daily        {"code","date_start","date_end"}
                                       -> {"daily_quotes": [...], "pagination_key"?}
    POST /v1/topics/daily/history      {"pagination_key": ...}
日次スナップショットに出来高は無い（volume=0 になる）。
応答形式が変わった場合の修正はこのファイルに集約する。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from strata.data.base import (
    DailyBar,
    DataProviderError,
    MarketDataProvider,
    to_dot_symbol,
)

DEFAULT_BASE_URL = "https://api.jquants.com"


class JQuantsProvider(MarketDataProvider):
    """J-Quants API クライアント（transport注入でオフラインテスト可能）."""

    name = "jquants"

    def __init__(
        self,
        app_id: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not app_id:
            raise DataProviderError("J-Quants app_id（APIキー）が未設定です")
        self._app_id = app_id
        self._base_url = base_url.rstrip("/")
        self._jwt: str | None = None
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ #

    def _authenticate(self) -> str:
        if self._jwt is not None:
            return self._jwt
        resp = self._client.post(
            f"{self._base_url}/v1/auth/tokens/login", json={"appid": self._app_id}
        )
        if resp.status_code != 201 and resp.status_code != 200:
            raise DataProviderError(f"J-Quants認証に失敗 (HTTP {resp.status_code})")
        try:
            jwt = str(resp.json()["JWT"])
        except (ValueError, KeyError) as exc:
            raise DataProviderError("J-Quants認証応答にJWTがありません") from exc
        self._jwt = jwt
        return jwt

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        jwt = self._authenticate()
        headers = {"Authorization": f"Bearer {jwt}"}
        body: dict[str, str] = {
            "code": to_dot_symbol(symbol),
            "date_start": start.isoformat(),
            "date_end": end.isoformat(),
        }
        raw: list[dict[str, Any]] = []
        while True:
            resp = self._client.post(
                f"{self._base_url}/v1/topics/daily", json=body, headers=headers
            )
            if resp.status_code == 401:  # トークン期限切れは1回だけ再発行
                self._jwt = None
                return self.fetch_daily(symbol, start, end)
            if resp.status_code != 200:
                raise DataProviderError(f"J-Quants日次取得に失敗 (HTTP {resp.status_code})")
            try:
                payload = resp.json()
            except ValueError as exc:
                raise DataProviderError("J-Quants応答がJSONではありません") from exc
            raw.extend(payload.get("daily_quotes", []))
            page_key = payload.get("pagination_key")
            if not page_key:
                break
            body = {"pagination_key": page_key}

        bars: list[DailyBar] = []
        for row in raw:
            try:
                bars.append(
                    DailyBar(
                        symbol=str(row["code"]).split(".")[0],
                        date=str(row["date"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=0,  # 日次スナップショットに出来高なし
                        source="jquants",
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DataProviderError(f"J-Quants応答の形式が不正です: {row!r}") from exc
        bars.sort(key=lambda b: b.date)
        return bars
