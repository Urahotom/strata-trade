"""カテゴリ別レートリミッタ（トークンバッケット）.

公式上限: 発注 5件/秒・情報 10件/秒・歩み値 2件/秒。
設定で安全側（既定 3/8/1 件/秒）に構成し、clock/sleep を注入可能にして
テストで厳密に検証できるようにする。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum


class ApiCategory(StrEnum):
    #: 発注・取消（公式上限 5件/秒）
    ORDER = "order"
    #: 情報・余力・銘柄登録（公式上限 10件/秒）
    INFO = "info"
    #: 歩み値（公式上限 2件/秒）
    TIMEANDSALES = "timeandsales"


#: 公式の絶対上限（設定値のバリデーションにも使用）
OFFICIAL_MAX_PER_S: dict[ApiCategory, float] = {
    ApiCategory.ORDER: 5.0,
    ApiCategory.INFO: 10.0,
    ApiCategory.TIMEANDSALES: 2.0,
}


class TokenBucket:
    """sleepして_wait_を許すトークンバッケット.

    Args:
        rate_per_s: 毎秒の許可件数
        capacity: バケット容量（既定1=バースト不可。レート厳守）
        clock: 現在時刻取得関数（テスト注入用）
        sleep: スリープ関数（テスト注入用）
    """

    def __init__(
        self,
        rate_per_s: float,
        capacity: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s は正の数")
        if capacity < 1:
            raise ValueError("capacity は 1 以上")
        self._rate = rate_per_s
        self._capacity = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    def try_acquire(self) -> bool:
        """1件取得できるなら取得してTrue。ダメなら取得しない（待たない）."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def acquire(self) -> float:
        """1件取得できるまで待機する。待機時間を返す（0以上）."""
        waited_total = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited_total
                wait = (1.0 - self._tokens) / self._rate
            self._sleep(wait)
            waited_total += wait


class RateLimiterRegistry:
    """APIカテゴリ別のバッケット管理."""

    def __init__(
        self,
        rates: dict[ApiCategory, float],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        for category, rate in rates.items():
            official = OFFICIAL_MAX_PER_S[category]
            if rate > official:
                raise ValueError(
                    f"{category} のレート {rate} は公式上限 {official}/秒 を超えられない"
                )
        self._buckets = {
            category: TokenBucket(rate, clock=clock, sleep=sleep)
            for category, rate in rates.items()
        }

    def acquire(self, category: ApiCategory) -> float:
        """カテゴリの送信枠が空くまで待機し、待機時間を返す."""
        return self._buckets[category].acquire()

    def bucket(self, category: ApiCategory) -> TokenBucket:
        return self._buckets[category]
