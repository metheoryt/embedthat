import redis.asyncio as redis
from aiogram import Bot
from aiogram.types import Message
from redis.asyncio import Redis

from bot.config import settings
from bot.enum import LinkOrigin
from bot.events.signals import (
    on_link_received,
    on_social_video_fail,
    on_social_video_sent,
    on_yt_video_fail,
    on_yt_video_sent,
    signal_handler,
)
from bot.util.redis import redis_client
from bot.util.social.schema import SocialVideoData
from bot.util.youtube.schema import YouTubeVideoData

_TTL = 90 * 24 * 3600  # 90 days


def _today() -> str:
    return settings.now().strftime("%Y-%m-%d")


async def _incr(client: Redis, key: str) -> None:
    await client.incr(key)
    await client.expire(key, _TTL)


async def _sadd(client: Redis, key: str, value: str) -> None:
    await client.sadd(key, value)
    await client.expire(key, _TTL)


@signal_handler(on_link_received)
async def stats_link_received(message: Message, origin: LinkOrigin) -> None:
    # Only ever fired from the aiogram process's own persistent event loop,
    # so the shared singleton is safe here (unlike the four handlers below).
    if not message.from_user:
        return
    d = _today()
    lang = (message.from_user.language_code or "unknown").lower()
    await _incr(redis_client, f"stats:{d}:requests")
    await _sadd(redis_client, f"stats:{d}:users", str(message.from_user.id))
    await _incr(redis_client, f"stats:{d}:chat:{message.chat.type}")
    await _incr(redis_client, f"stats:{d}:lang:{lang}")


# The four handlers below receive signals that are also fired from Dramatiq
# actors (bot/worker/actors.py), where each job runs under its own
# short-lived asyncio.run() event loop. Reusing the shared redis_client
# singleton there breaks on a worker's second job (see the Global
# Constraints amendment in the dramatiq-task-queue plan) — each handler
# opens and closes its own connection instead.


@signal_handler(on_yt_video_sent)
async def stats_yt_sent(
    link: str,
    chat_id: int,
    chat_type: str,
    bot: Bot,
    video: YouTubeVideoData,
    fresh: bool,
) -> None:
    client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        await _incr(client, f"stats:{_today()}:success:youtube")
    finally:
        await client.aclose()


@signal_handler(on_social_video_sent)
async def stats_social_sent(
    link: str,
    chat_id: int,
    chat_type: str,
    bot: Bot,
    video: SocialVideoData,
    fresh: bool,
) -> None:
    client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        platform = (video.origin or "social").lower()
        await _incr(client, f"stats:{_today()}:success:{platform}")
    finally:
        await client.aclose()


@signal_handler(on_yt_video_fail)
async def stats_yt_fail(link: str) -> None:
    client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        await _incr(client, f"stats:{_today()}:fail:youtube")
    finally:
        await client.aclose()


@signal_handler(on_social_video_fail)
async def stats_social_fail(link: str) -> None:
    client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        await _incr(client, f"stats:{_today()}:fail:social")
    finally:
        await client.aclose()
