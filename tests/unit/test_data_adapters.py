"""J-Quants / yfinance アダプタのテスト（MockTransport で完全オフライン）."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from strata.data.base import DataProviderError
from strata.data.jquants import JQuantsProvider
from strata.data.yfinance import YFinanceProvider


class TestJQuants:
    def _provider(self, responses: list[httpx.Response]) -> JQuantsProvider:
        it = iter(responses)

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/auth/tokens/login"):
                return httpx.Response(201, json={"JWT": "tok-123"})
            return next(it)

        return JQuantsProvider("app-key", transport=httpx.MockTransport(_handle))

    def test_認証から日次取得まで(self) -> None:
        body = {
            "daily_quotes": [
                {
                    "code": "8306.T",
                    "date": "2026-10-21",
                    "open": 100,
                    "high": 110,
                    "low": 95,
                    "close": 105,
                },
                {
                    "code": "8306.T",
                    "date": "2026-10-20",
                    "open": 99,
                    "high": 101,
                    "low": 98,
                    "close": 100,
                },
            ]
        }
        provider = self._provider([httpx.Response(200, json=body)])
        bars = provider.fetch_daily("8306", date(2026, 10, 19), date(2026, 10, 21))
        assert [b.date for b in bars] == ["2026-10-20", "2026-10-21"]  # 昇順
        assert bars[-1].close == 105
        assert bars[-1].source == "jquants"
        assert bars[-1].volume == 0  # 日次は出来高なし
        provider.close()

    def test_ページネーションを辿る(self) -> None:
        page1 = httpx.Response(
            200,
            json={
                "daily_quotes": [
                    {
                        "code": "8306.T",
                        "date": "2026-10-20",
                        "open": 1,
                        "high": 1,
                        "low": 1,
                        "close": 1,
                    }
                ],
                "pagination_key": "p2",
            },
        )
        page2 = httpx.Response(
            200,
            json={
                "daily_quotes": [
                    {
                        "code": "8306.T",
                        "date": "2026-10-21",
                        "open": 2,
                        "high": 2,
                        "low": 2,
                        "close": 2,
                    }
                ]
            },
        )
        provider = self._provider([page1, page2])
        bars = provider.fetch_daily("8306", date(2026, 10, 1), date(2026, 10, 31))
        assert len(bars) == 2
        provider.close()

    def test_認証失敗はDataProviderError(self) -> None:
        def _handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={})

        provider = JQuantsProvider("bad-key", transport=httpx.MockTransport(_handle))
        with pytest.raises(DataProviderError):
            provider.fetch_daily("8306", date(2026, 10, 1), date(2026, 10, 2))

    def test_空app_idは即エラー(self) -> None:
        with pytest.raises(DataProviderError):
            JQuantsProvider("")


class TestYFinance:
    def _chart_payload(self) -> dict:
        # 2026-10-20, 21 (JST) 相当のタイムスタンプ
        return {
            "chart": {
                "result": [
                    {
                        "meta": {"symbol": "8306.T"},
                        "timestamp": [1792425600, 1792512000],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100, 105],
                                    "high": [110, 108],
                                    "low": [95, 100],
                                    "close": [105, 106],
                                    "volume": [1000, 2000],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }

    def test_日足変換と気配(self) -> None:
        def _handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._chart_payload())

        provider = YFinanceProvider(transport=httpx.MockTransport(_handle))
        bars = provider.fetch_daily("8306", date(2026, 10, 19), date(2026, 10, 21))
        assert len(bars) == 2
        assert bars[0].source == "yfinance"
        assert bars[0].close == 105
        assert bars[0].volume == 1000
        provider.close()

    def test_nullを含むレコードはスキップ(self) -> None:
        payload = self._chart_payload()
        quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
        quote["open"][0] = None  # 欠損レコード
        provider = YFinanceProvider(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        )
        bars = provider.fetch_daily("8306", date(2026, 10, 19), date(2026, 10, 21))
        assert len(bars) == 1
        provider.close()

    def test_エラー応答はDataProviderError(self) -> None:
        payload = {"chart": {"result": None, "error": {"code": "Bad Request"}}}
        provider = YFinanceProvider(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        )
        with pytest.raises(DataProviderError):
            provider.fetch_daily("8306", date(2026, 10, 19), date(2026, 10, 21))

    def test_HTTPエラーはDataProviderError(self) -> None:
        provider = YFinanceProvider(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        with pytest.raises(DataProviderError):
            provider.fetch_daily("8306", date(2026, 10, 19), date(2026, 10, 21))
