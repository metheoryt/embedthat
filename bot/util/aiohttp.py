import logging
from typing import Any

import aiohttp

from bot.dispatcher import dp

log = logging.getLogger(__name__)

session: aiohttp.ClientSession = aiohttp.ClientSession()


@dp.shutdown()
async def on_shutdown(*args: Any, **kwargs: Any) -> None:
    await session.close()
    log.info("aiohttp session has been closed")
