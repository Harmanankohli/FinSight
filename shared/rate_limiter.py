"""Async token-bucket rate limiter.

Prevents IP bans from SEC EDGAR (10 req/s hard limit) and Yahoo Finance 429s.
"""
import asyncio
import time


class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate: float = rate
        self.burst: int = burst
        self.tokens: float = float(burst)
        self.last: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.burst,
                    self.tokens + (now - self.last) * self.rate,
                )
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                deficit = 1 - self.tokens
            await asyncio.sleep(deficit / self.rate)
