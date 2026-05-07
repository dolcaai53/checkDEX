from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TypeVar

import aiohttp

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Exceptions that warrant an automatic retry
_RETRYABLE = (
    aiohttp.ClientConnectionError,
    aiohttp.ServerTimeoutError,
    aiohttp.ServerDisconnectedError,
    asyncio.TimeoutError,
)


async def with_retry(
    fn: Callable[[], Coroutine[None, None, T]],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    label: str = "",
) -> T:
    """Call *fn* and retry on transient network errors with exponential backoff.

    HTTP 429 from the exchange is treated as a retryable error with a longer
    initial delay (30 s) to respect the rate limit.
    """
    delay = base_delay
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except aiohttp.ClientResponseError as exc:
            if exc.status == 429:
                wait = max(30.0, delay)
                if attempt < max_retries:
                    logger.warning(
                        "Rate limit hit, retrying %s",
                        label,
                        extra={"attempt": attempt + 1, "wait_seconds": wait},
                    )
                    await asyncio.sleep(wait)
                    delay = min(delay * 2, max_delay)
                    continue
            raise
        except _RETRYABLE as exc:
            if attempt >= max_retries:
                raise
            wait = min(delay, max_delay)
            logger.warning(
                "Transient error in %s, retrying",
                label,
                extra={"attempt": attempt + 1, "wait_seconds": wait, "error": str(exc)},
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, max_delay)

    raise RuntimeError("unreachable")
