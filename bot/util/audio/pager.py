import json
import logging

import redis.asyncio as redis
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from bot.util.audio.schema import AudioRequestData

log = logging.getLogger(__name__)


def _messages_key(chat_id: int, root_message_id: int) -> str:
    return f"da:msgs:{chat_id}:{root_message_id}"


class StaleFileIdsError(Exception):
    """Telegram rejected a cached page. By the time this is raised the page's file
    ids are already cleared from `da:` and from the per-track `au:` keys, so a
    retry downloads instead of hitting the same dead ids."""


async def invalidate_page(redis_client: redis.Redis, audio: AudioRequestData, page: int) -> None:
    for track in audio.page(page):
        if track.file_id:
            await redis_client.delete(track.cache_key)
            track.file_id = None
    await redis_client.set(audio.cache_key, audio.model_dump_json())


async def redeliver_page(
    redis_client: redis.Redis, bot: Bot, chat_id: int, root_message_id: int, audio: AudioRequestData, page: int,
) -> None:
    key = _messages_key(chat_id, root_message_id)
    old_raw = await redis_client.get(key)
    old_ids: list[int] = json.loads(old_raw) if old_raw else []

    try:
        new_ids = await audio.send_to_chat(bot, chat_id, reply_to_message_id=root_message_id, page=page)
    except TelegramBadRequest as e:
        log.info("cached page %d of %s rejected (%s), clearing its file ids", page, audio.cache_key, e)
        await invalidate_page(redis_client, audio, page)
        raise StaleFileIdsError(audio.cache_key) from e
    await redis_client.set(key, json.dumps(new_ids))

    for message_id in old_ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            log.warning("could not delete old page message %s in chat %s", message_id, chat_id)
