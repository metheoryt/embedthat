import asyncio
import logging
from types import TracebackType

from redis.asyncio.lock import Lock

log = logging.getLogger(__name__)


class HeartbeatLock:
    """
    Wraps redis.asyncio.lock.Lock and periodically reacquires it while held.

    redis-py's Lock sets a static TTL at acquire time and never renews it.
    If the code inside the lock runs longer than `timeout`, Redis expires the
    key mid-processing: a concurrent request can then grab the "freed" lock
    and duplicate the work, and the original release() raises
    LockNotOwnedError once processing finally finishes. The heartbeat resets
    the TTL well before it would expire, so long-running work stays covered.
    """

    def __init__(self, lock: Lock, interval: float | None = None) -> None:
        self._lock = lock
        self._interval = interval or max(lock.timeout / 3, 0.1)
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "HeartbeatLock":
        await self._lock.__aenter__()
        self._task = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        await self._lock.__aexit__(exc_type, exc, tb)

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self._lock.reacquire()
        except asyncio.CancelledError:
            pass
