import asyncio
import time
import pytest
from shared.rate_limiter import TokenBucket


async def test_burst_consumed_quickly():
    """burst tokens are acquired immediately without waiting."""
    bucket = TokenBucket(rate=10.0, burst=5)
    t0 = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - t0 < 0.5


async def test_rate_enforced_after_burst():
    """After draining the burst, each additional token costs ~1/rate seconds."""
    bucket = TokenBucket(rate=20.0, burst=2)
    await bucket.acquire()
    await bucket.acquire()
    t0 = time.monotonic()
    await bucket.acquire()  # should wait ~50ms
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.03, f"Expected >=30ms, got {elapsed*1000:.1f}ms"


async def test_tokens_refill_over_time():
    """After waiting, the bucket refills and acquires quickly again."""
    bucket = TokenBucket(rate=20.0, burst=2)
    await bucket.acquire()
    await bucket.acquire()
    await asyncio.sleep(0.12)  # 0.12s * 20/s = 2.4 tokens refilled
    t0 = time.monotonic()
    await bucket.acquire()
    await bucket.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"Refilled tokens should be fast, got {elapsed*1000:.1f}ms"


async def test_single_token_bucket():
    """A burst=1 bucket forces a wait between every call."""
    bucket = TokenBucket(rate=50.0, burst=1)
    await bucket.acquire()  # use the single burst token
    t0 = time.monotonic()
    await bucket.acquire()  # must wait ~20ms (1/50s)
    assert time.monotonic() - t0 >= 0.01
