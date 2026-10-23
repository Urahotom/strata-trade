"""Yahoo Finance chart API アダプタ（yfinanceパッケージ非依存の自作実装）.

    GET {base}/v8/finance/chart/{code}.T?period1=..&period2=..&interval=1d

不整合の検出しやすい自作パーサのため、外部ライブラリを使わず
httpx + pydantic で直接扱う。test では transport を注入する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import httpx

from strata.data.base import (
    DailyBar,
    DataProviderError,
    MarketDataProvider,
    to_dot_symbol,
)

DEFAULT_BASE_URL = "https://query1.finance.yahoo.com"
_JST = timezone(timedelta(hours=9))


def _to_epoch(d: date, *, end_of_day: bool = False) -> int:
    """JSTの日付をepoch秒へ（period2は終端exclusiveなので翌日扱いに注意）."""
    dt = datetime(d.year, d.month, d.day, tzinfo=_JST)
    if end_of_day:
        dt += timedelta(days=1)
    return int(dt.timestamp())


class YFinanceProvider(MarketDataProvider):
    """Yahoo Finance 日足クライアント（transport注入でオフラインテスト可能）."""

    name = "yfinance"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": "strata-trade/0.0 (chart API client)"},
        )

    def close(self) -> None:
        self._client.close()

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        params = {
            "period1": str(_to_epoch(start)),
            "period2": str(_to_epoch(end, end_of_day=True)),
            "interval": "1d",
            "events": "div,splits",
            "includePrePost": "false",
        }
        url = f"{self._base_url}/v8/finance/chart/{to_dot_symbol(symbol)}"
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise DataProviderError(f"Yahoo取得に失敗しました: {exc}") from exc
        if resp.status_code != 200:
            raise DataProviderError(f"Yahoo取得に失敗 (HTTP {resp.status_code})")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataProviderError("Yahoo応答がJSONではありません") from exc

        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise DataProviderError(f"Yahooエラー: {chart['error']}")
        results = chart.get("result")
        if not results:
            raise DataProviderError("Yahoo応答に chart.result がありません")
        result = results[0]
        meta = result.get("meta") or {}
        code = str(meta.get("symbol", to_dot_symbol(symbol))).split(".")[0]
        timestamps = result.get("timestamp") or []
        try:
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError) as exc:
            raise DataProviderError("Yahoo応答に price レイアウトがありません") from exc
        #: 分割・配当で調整済みの close があればそれを優先する。
        #: 生 close は株式分割日を真実の -90% 暴落として拾ってしまい
        #: バックテストが完全に壊れる（F-004 正誤参照）ため必須。
        adj_close: list[object] | None = None
        try:
            adj_raw = result["indicators"]["adjclose"][0]["adjclose"]
            adj_close = list(adj_raw) if adj_raw is not None else None
        except (KeyError, IndexError, TypeError):
            adj_close = None

        bars: list[DailyBar] = []
        for i, ts in enumerate(timestamps):
            try:
                o = quote["open"][i]
                h = quote["high"][i]
                low = quote["low"][i]
                c = quote["close"][i]
                if adj_close is not None:
                    c = adj_close[i]
            except (KeyError, IndexError, TypeError) as exc:
                raise DataProviderError(f"Yahoo応答の列長が不揃いです (index={i})") from exc
            if o is None or h is None or low is None or c is None:
                continue  # 休場・部分欠損レコードはスキップ（欠損検知はバリデータ）
            volume_raw = (quote.get("volume") or [None] * len(timestamps))[i]
            day = datetime.fromtimestamp(int(ts), UTC).astimezone(_JST).date()
            bars.append(
                DailyBar(
                    symbol=code,
                    date=day.isoformat(),
                    open=float(o),
                    high=float(h),
                    low=float(low),
                    close=float(c),
                    volume=int(volume_raw or 0),
                    source="yfinance",
                )
            )
        bars.sort(key=lambda b: b.date)
        return bars
