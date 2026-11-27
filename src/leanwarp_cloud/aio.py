"""An asyncio face on the client: each call runs in a worker thread.

Every method of `LeanWarpCloud` is available as a coroutine with the same
arguments, retries, deadlines and result. Calls made together run together, up
to ``concurrency`` at a time, over one connection pool. Cancelling a coroutine
stops waiting for its call; the call itself still ends by its own deadline and
keeps its place in ``concurrency`` until then. ``close`` waits for such calls.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Awaitable, Callable
from typing import Any

from .client import LeanWarpCloud
from .errors import UsageError

# Context-manager and lifecycle methods are the async client's own.
_OWN = frozenset({"close"})


class AsyncLeanWarpCloud:
    """``await client.check(...)`` for every ``LeanWarpCloud`` method.

    Accepts `LeanWarpCloud`'s arguments, or wraps an existing client.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: LeanWarpCloud | None = None,
        concurrency: int = 16,
        **options: Any,
    ) -> None:
        if not 1 <= concurrency <= 100:
            raise UsageError("concurrency must be between 1 and 100")
        if client is not None and (api_key is not None or options):
            raise UsageError("pass either a client or its arguments, not both")
        self.client = client if client is not None else LeanWarpCloud(api_key, **options)
        self._concurrency = concurrency
        self._slots: asyncio.Semaphore | None = None
        # Calls whose threads have not finished, and whether close has begun.
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        method = getattr(LeanWarpCloud, name, None)
        if name.startswith("_") or name in _OWN or not callable(method):
            raise AttributeError(name)

        @functools.wraps(method)
        async def call(*args: Any, **kwargs: Any) -> Any:
            if self.closing():
                raise UsageError("the client is closed")
            if self._slots is None:
                self._slots = asyncio.Semaphore(self._concurrency)
            slots, loop = self._slots, asyncio.get_running_loop()
            # Counted from admission, so close also waits for calls queued for a slot.
            self._active += 1
            self._idle.clear()

            def leave() -> None:
                self._active -= 1
                if not self._active:
                    self._idle.set()

            try:
                await slots.acquire()
            except BaseException:
                leave()
                raise
            if self.closing():
                slots.release()
                leave()
                raise UsageError("the client is closed")

            def finished() -> None:
                slots.release()
                leave()

            def work() -> Any:
                # The slot follows the thread, not the await: a cancelled caller's
                # call keeps its place until it ends by its own deadline.
                try:
                    return method(self.client, *args, **kwargs)
                finally:
                    loop.call_soon_threadsafe(finished)

            # Shielded, so a cancellation never stops the work before it starts:
            # its thread always runs, and always gives the slot back.
            # The caller's context variables go with it, as asyncio.to_thread does.
            context = contextvars.copy_context()
            return await asyncio.shield(loop.run_in_executor(None, context.run, work))

        return call

    def closing(self) -> bool:
        """Whether close has begun: new calls, and calls still queued, are refused."""
        return self._closing

    async def close(self) -> None:
        """Refuse new calls, wait for running ones to end, then close the pool."""
        self._closing = True
        await asyncio.shield(self._idle.wait())
        await asyncio.to_thread(self.client.close)

    async def __aenter__(self) -> AsyncLeanWarpCloud:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()
