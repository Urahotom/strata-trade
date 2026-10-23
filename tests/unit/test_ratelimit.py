"""レートリミッタ（トークンバッケット）のテスト（偽時計で厳密検証）."""

from __future__ import annotations

import pytest

from strata.api.ratelimit import ApiCategory, RateLimiterRegistry, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds  # sleepで時刻を進める


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def test_レートどおりに間隔が開く(clock: FakeClock) -> None:
    bucket = TokenBucket(5.0, clock=clock.time, sleep=clock.sleep)
    assert bucket.try_acquire()  # 初期トークン1
    assert not bucket.try_acquire()  # バースト不可
    clock.now += 0.2  # 1/5秒 = 1件分
    assert bucket.try_acquire()
    assert not bucket.try_acquire()


def test_acquireは必要分だけ待つ(clock: FakeClock) -> None:
    bucket = TokenBucket(2.0, clock=clock.time, sleep=clock.sleep)
    assert bucket.acquire() == 0.0  # 初期トークン
    waited = bucket.acquire()
    assert waited == pytest.approx(0.5)  # 2件/秒 = 0.5秒待ち
    assert sum(clock.slept) == pytest.approx(0.5)
    # 実際に待機分だけ時刻が進んでいる（=待ったことになる）
    assert clock.now == pytest.approx(1000.5)


def test_連続acquireで待機が蓄積(clock: FakeClock) -> None:
    bucket = TokenBucket(10.0, clock=clock.time, sleep=clock.sleep)
    total = sum(bucket.acquire() for _ in range(5))
    assert total == pytest.approx(0.4)  # 4回 x 0.1秒


def test_バーストは容量まで(clock: FakeClock) -> None:
    bucket = TokenBucket(1.0, capacity=3, clock=clock.time, sleep=clock.sleep)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()


def test_不正レートは拒否() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)
    with pytest.raises(ValueError):
        TokenBucket(-1)


def test_レジトリミッタは公式上限を超えられない() -> None:
    with pytest.raises(ValueError, match="公式上限"):
        RateLimiterRegistry({ApiCategory.ORDER: 6.0})
    with pytest.raises(ValueError, match="公式上限"):
        RateLimiterRegistry({ApiCategory.INFO: 11.0})
    with pytest.raises(ValueError, match="公式上限"):
        RateLimiterRegistry({ApiCategory.TIMEANDSALES: 3.0})


def test_レジトリミッタはカテゴリ別で独立(clock: FakeClock) -> None:
    registry = RateLimiterRegistry(
        {ApiCategory.ORDER: 5.0, ApiCategory.INFO: 10.0, ApiCategory.TIMEANDSALES: 2.0},
        clock=clock.time,
        sleep=clock.sleep,
    )
    # 発注を1件消費しても情報は使える
    assert registry.bucket(ApiCategory.ORDER).try_acquire()
    assert registry.bucket(ApiCategory.INFO).try_acquire()
    assert registry.bucket(ApiCategory.TIMEANDSALES).try_acquire()
    assert not registry.bucket(ApiCategory.ORDER).try_acquire()
