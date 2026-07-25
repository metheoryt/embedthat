import logging
from typing import Any

import redis.asyncio as redis

from bot.config import settings
from bot.dispatcher import dp

log = logging.getLogger(__name__)

redis_client: redis.Redis = redis.from_url(
    str(settings.redis_dsn), decode_responses=True
)


@dp.shutdown()
async def on_shutdown(*args: Any, **kwargs: Any) -> None:
    await redis_client.aclose()
    log.info("redis client has been closed")
