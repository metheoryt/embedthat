from pydantic import BaseModel
from redis.asyncio import Redis


class Waiter(BaseModel):
    chat_id: int
    chat_type: str
    reply_to_message_id: int  # for audio-pager waiters, this is the pager's root message id
    ack_message_id: int | None = None


def _waiters_key(cache_key: str) -> str:
    return f"{cache_key}:waiters"


async def register_waiter(redis_client: Redis, cache_key: str, waiter: Waiter, ttl: int) -> bool:
    """
    Registers a waiter for the given cache key. Returns True if this waiter
    is the first one registered (the caller should enqueue the processing
    job in that case); False if a job for this cache key is already in
    flight (the caller should not enqueue a second one).
    """
    key = _waiters_key(cache_key)
    length = await redis_client.rpush(key, waiter.model_dump_json())
    await redis_client.expire(key, ttl)
    return length == 1


async def pop_waiters(redis_client: Redis, cache_key: str) -> list[Waiter]:
    """Atomically reads and clears all waiters registered for the given cache key."""
    key = _waiters_key(cache_key)
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        raw_entries, _ = await pipe.execute()
    return [Waiter.model_validate_json(raw) for raw in raw_entries]


async def clear_waiters(redis_client: Redis, cache_key: str) -> None:
    """
    Drops every waiter registered for the key, rolling a registration back
    when its job never reached the queue. The list is exactly what makes a
    repeated link a no-op, so an orphaned one wedges that link until the TTL
    expires -- no job in flight, no error, and every retry silently skipped.
    """
    await redis_client.delete(_waiters_key(cache_key))
