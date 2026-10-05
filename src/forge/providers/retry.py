"""Retry retryable provider errors with exponential backoff (docs/CONTRACTS.md)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from forge.providers.base import RETRYABLE, ProviderError, StreamItem

RETRY_DELAYS: tuple[float, ...] = (1, 2, 4, 8, 16)
MAX_RETRY_AFTER_S = 60.0

Sleep = Callable[[float], Awaitable[None]]


async def stream_with_retries(
    attempt: Callable[[], AsyncIterator[StreamItem]],
    delays: Sequence[float] = RETRY_DELAYS,
    sleep: Sleep = asyncio.sleep,
) -> AsyncIterator[StreamItem]:
    """Run `attempt`; retry a retryable error if it happens before the first item."""
    for wait in [*delays, None]:
        started = False
        try:
            async for item in attempt():
                started = True
                yield item
            return
        except ProviderError as err:
            # Text already shown cannot be taken back, so a mid-stream failure is final here.
            if started or wait is None or err.kind not in RETRYABLE:
                raise
            delay = err.retry_after_s if err.retry_after_s is not None else wait
            if delay > MAX_RETRY_AFTER_S:
                raise
            await sleep(delay)
