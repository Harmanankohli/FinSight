"""Async token-bucket rate limiter.

Prevents IP bans from SEC EDGAR (10 req/s hard limit) and Yahoo Finance 429s.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class TokenBucket:
    def __init__(self, rate: float, burst: int, name: str = "") -> None:
        self.rate: float = rate
        self.burst: int = burst
        self.tokens: float = float(burst)
        self.last: float = time.monotonic()
        self._name: str = name
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
            logger.debug(
                "Rate limiter sleeping %.2fs (%.1f tokens available)",
                deficit / self.rate,
                self.tokens,
            )
            await asyncio.sleep(deficit / self.rate)

    async def try_acquire(self) -> bool:
        """Non-blocking equivalent of acquire().

        Returns True if a token was acquired, False if rate-limited.
        Never sleeps.
        """
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(
                self.burst,
                self.tokens + (now - self.last) * self.rate,
            )
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            logger.debug("Rate limiter hit for %s (rate=%.1f/s, burst=%d)", self._name, self.rate, self.burst)
            return False
