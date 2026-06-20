"""Async token-bucket rate limiter.

Prevents IP bans from SEC EDGAR (10 req/s hard limit) and Yahoo Finance 429s.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class TokenBucket:
    """Async token-bucket rate limiter for external API calls.

    Maintains a token count that refills at `rate` tokens/second up to `burst`
    capacity.  `acquire()` blocks until a token is available; `try_acquire()`
    returns False immediately if rate-limited.  Thread-safe via asyncio.Lock.

    Used to enforce SEC EDGAR's 10 req/s limit, Yahoo Finance rate caps, and
    any other external API that throttles on excessive request frequency.
    """

    def __init__(self, rate: float, burst: int, name: str = "") -> None:
        """Initialise bucket with refill rate and burst capacity.

        Args:
            rate: Token refill rate per second (e.g. 10.0 for 10 req/s).
            burst: Maximum accumulated tokens (peak burst size).
            name: Optional label for debug logging.
        """
        self.rate: float = rate
        self.burst: int = burst
        self.tokens: float = float(burst)
        self.last: float = time.monotonic()
        self._name: str = name
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one.

        Sleeps for (deficit / rate) seconds when the bucket is empty.
        Logs wait duration at DEBUG level.
        """
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
            logger.debug(
                "Rate limiter hit for %s (rate=%.1f/s, burst=%d)", self._name, self.rate, self.burst
            )
            return False
