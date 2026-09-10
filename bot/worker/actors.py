import asyncio
import logging
import tempfile
from pathlib import Path
from typing import NoReturn

import dramatiq
import redis.asyncio as redis
from aiogram import Bot, types
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from dramatiq.middleware import CurrentMessage
from redis.asyncio.lock import Lock

from bot.config import settings
from bot.events.signals import on_social_video_sent, on_yt_video_sent
from bot.util.audio.download import probe_link
from bot.util.audio.exc import AudioDownloadError
from bot.util.audio.pager import redeliver_page
from bot.util.audio.schema import AudioRequestData, AudioTrackData
from bot.util.chat import is_group_chat
from bot.util.redis_lock import HeartbeatLock
from bot.util.social.exc import SocialDownloadError
from bot.util.social.schema import SocialVideoData
from bot.util.youtube.enum import TargetLang
from bot.util.youtube.exc import YouTubeError
from bot.util.youtube.schema import YouTubeVideoData
from bot.util.youtube.video import get_audio_stream
from bot.util.ytdlp import TransientDownloadError, is_login_wall
from bot.worker.broker import (
    broker,  # noqa: F401 -- registers the Redis broker before actors are declared
)
from bot.worker.chat_action import with_chat_action
from bot.worker.error_reporting import (
    report_actor_failure,  # noqa: F401 -- registers the actor with the broker
)
from bot.worker.pipeline import (
    handle_audio_page,
    handle_social_video,
    handle_youtube_video,
)
from bot.worker.waiters import Waiter, pop_waiters

log = logging.getLogger(__name__)

# Kept as a constant because `_is_final_attempt` has to compare against the very
# number the decorator below was given -- reading it back off the actor works but
# goes through untyped dramatiq internals.
_SOCIAL_MAX_RETRIES = 2

_COOKIE_ALERT_KEY = "cookies:stale-alerted"
_COOKIE_ALERT_TTL = 24 * 60 * 60


async def _alert_if_cookies_stale(redis_client: redis.Redis, url: str, error: Exception) -> None:
    """Raise a CRITICAL -- and so an admin-chat message -- when a login wall is
    hit *while `COOKIES_FILE` is set*, which means the jar is not doing its job
    and a human has to act.

    Two distinct causes, and the message must name the right one: the file is
    missing (never exported, or the mount points somewhere else), or it is there
    and the site rejected it (expired, or the account was challenged). Collapsing
    them into "re-export it" sends the operator to re-export a jar that was never
    exported; gating the whole alert on `.exists()` instead is worse still --
    `cookie_opts()` logs the missing file at WARNING, and WARNING never reaches
    `ADMIN_CHAT_ID`, which is exactly how an empty `./cookies` mount sat unnoticed
    from 2026-08-16 to 2026-09-07 while every login-walled link failed.

    Silent when no cookies are configured: a login wall is then just a link we
    were never going to be able to fetch, and says nothing a human can act on.

    Rate-limited to one alert per day via a Redis NX+TTL key, because legitimately
    private posts raise the same error and would otherwise flood the chat --
    the exact failure mode the CRITICAL-only threshold exists to prevent. One key
    covers both causes: they are mutually exclusive at any moment and the fix is
    the same errand.
    Cannot ride on `report_actor_failure`: these actors list both download
    errors in `throws`, so dramatiq's Retries middleware never invokes it.
    """
    if not settings.cookies_file or not is_login_wall(error):
        return
    if not await redis_client.set(_COOKIE_ALERT_KEY, "1", ex=_COOKIE_ALERT_TTL, nx=True):
        return
    if not settings.cookies_file.exists():
        log.critical(
            "login wall hit and COOKIES_FILE points at %s, but there is NO SUCH FILE -- "
            "every request has been running without cookies. Export a jar there (check the "
            "mount if you already did). Further cookie alerts suppressed for 24h.\nlink: %s\n%s",
            settings.cookies_file, url, error,
        )
        return
    log.critical(
        "login wall hit while cookies ARE configured (%s) -- the cookie jar has most "
        "likely gone stale; re-export it. Further cookie alerts suppressed for 24h.\nlink: %s\n%s",
        settings.cookies_file, url, error,
    )


def _is_final_attempt(max_retries: int) -> bool:
    """True when the message being processed has no dramatiq retry left.

    Reads the attempt counter off `CurrentMessage` (middleware installed in
    `bot.worker.broker`); dramatiq's Retries middleware bumps
    `options["retries"]` only when it re-enqueues, so during attempt N the
    counter still reads N. Outside a worker -- a probe script, a direct call --
    there is no message and no retry coming, so the caller must report at once.
    """
    message = CurrentMessage.get_current_message()
    if message is None:
        return True
    retries: int = (message.options or {}).get("retries", 0)
    return retries >= max_retries


async def _retry_or_report(
    bot: Bot,
    redis_client: redis.Redis,
    cache_key: str,
    error: TransientDownloadError,
    permanent: type[Exception],
    message: str,
    max_retries: int = _SOCIAL_MAX_RETRIES,
) -> NoReturn:
    """Handles a `TransientDownloadError`: re-raise it so dramatiq retries with
    backoff, or -- on the last attempt -- tell the waiters and convert it.

    The conversion matters. `permanent` is a class the actor lists in `throws=`,
    so dramatiq aborts the message without invoking `report_actor_failure`: a
    site that stayed unreachable for the whole retry budget is the user's
    problem to see, not a bug to page the admin about. Always raises.
    """
    if not _is_final_attempt(max_retries):
        raise error
    waiters = await _pop_waiters(redis_client, cache_key)
    await _notify_waiters_failure(bot, waiters, f"{message}: {error}")
    raise permanent(str(error)) from error


async def _safe_edit_ack(bot: Bot, chat_id: int, message_id: int | None, text: str) -> None:
    if message_id is None:
        return
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        log.warning("could not edit ack message %s in chat %s", message_id, chat_id)


async def _safe_delete_ack(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        log.warning("could not delete ack message %s in chat %s", message_id, chat_id)


async def _pop_waiters(redis_client: redis.Redis, cache_key: str) -> list[Waiter]:
    """pop_waiters, deduped by chat_id so a re-sent link/re-tapped button can't cause a double delivery."""
    waiters = await pop_waiters(redis_client, cache_key)
    seen: set[int] = set()
    deduped = []
    for waiter in waiters:
        if waiter.chat_id not in seen:
            seen.add(waiter.chat_id)
            deduped.append(waiter)
    return deduped


async def _notify_waiters_success(
    bot: Bot, waiters: list[Waiter], video: YouTubeVideoData | SocialVideoData
) -> None:
    for waiter in waiters:
        await _safe_delete_ack(bot, waiter.chat_id, waiter.ack_message_id)
        await video.send_to_chat(bot, waiter.chat_id, reply_to_message_id=waiter.reply_to_message_id)


async def _notify_waiters_failure(bot: Bot, waiters: list[Waiter], text: str) -> None:
    for waiter in waiters:
        if is_group_chat(waiter.chat_id):
            # stay quiet in groups/supergroups/channels — no failure chatter
            continue
        if waiter.ack_message_id is not None:
            await _safe_edit_ack(bot, waiter.chat_id, waiter.ack_message_id, text)
        else:
            await bot.send_message(waiter.chat_id, text, reply_to_message_id=waiter.reply_to_message_id)


async def _notify_audio_waiters_success(bot: Bot, waiters: list[Waiter], video: YouTubeVideoData) -> None:
    for waiter in waiters:
        await bot.send_audio(
            waiter.chat_id,
            video.audio_file_id,
            performer=video.author,
            title=video.title,
            duration=video.length,
        )


async def _resolve_cached_tracks(redis_client: redis.Redis, tracks: list[AudioTrackData]) -> None:
    """Fills in file_id/title/uploader/duration for any track already present in the
    per-track dedup cache, so handle_audio_page only downloads genuine misses."""
    for track in tracks:
        if track.file_id:
            continue
        cached_raw = await redis_client.get(track.cache_key)
        if not cached_raw:
            continue
        cached = AudioTrackData.model_validate_json(cached_raw)
        track.file_id = cached.file_id
        track.title = track.title or cached.title
        track.uploader = track.uploader or cached.uploader
        track.duration = track.duration or cached.duration


async def _save_tracks_to_cache(redis_client: redis.Redis, tracks: list[AudioTrackData]) -> None:
    for track in tracks:
        if track.file_id:
            await redis_client.set(track.cache_key, track.model_dump_json())


async def _notify_audio_page_waiters_success(
    redis_client: redis.Redis, bot: Bot, waiters: list[Waiter], audio: AudioRequestData, page: int,
) -> None:
    for waiter in waiters:
        await redeliver_page(redis_client, bot, waiter.chat_id, waiter.reply_to_message_id, audio, page)


@with_chat_action()
async def _process_youtube_link_async(bot: Bot, chat_id: int, link: str, target_lang_value: str) -> None:
    redis_client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        target_lang = TargetLang(target_lang_value)
        video = YouTubeVideoData.model_validate(dict(link=link, target_lang=target_lang))

        lock = Lock(redis_client, f'{video.cache_key}:lock', timeout=10 * 60, blocking_timeout=11 * 60)
        async with HeartbeatLock(lock):
            try:
                video = await handle_youtube_video(bot, video)
            except YouTubeError as e:
                waiters = await _pop_waiters(redis_client, video.cache_key)
                await _notify_waiters_failure(bot, waiters, f"❌ Couldn't process this video: {e}")
                raise

            await redis_client.set(video.cache_key, video.model_dump_json())
            log.info("cached %s (%d files)", video.cache_key, len(video.file_ids))

            waiters = await _pop_waiters(redis_client, video.cache_key)
            await _notify_waiters_success(bot, waiters, video)
            for waiter in waiters:
                await on_yt_video_sent.send(link, waiter.chat_id, waiter.chat_type, bot, video, True)
    finally:
        await redis_client.aclose()


@dramatiq.actor(
    max_retries=2,
    min_backoff=30_000,
    max_backoff=5 * 60_000,
    time_limit=45 * 60_000,
    throws=(YouTubeError,),
    on_retry_exhausted="report_actor_failure",
)
def process_youtube_link(chat_id: int, link: str, target_lang: str) -> None:
    bot = Bot(token=settings.bot_token)
    try:
        asyncio.run(_process_youtube_link_async(bot, chat_id, link, target_lang))
    finally:
        asyncio.run(bot.session.close())


@with_chat_action(ChatAction.UPLOAD_VOICE)
async def _process_youtube_audio_async(bot: Bot, chat_id: int, video_id: str, reply_to_message_id: int) -> None:
    redis_client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        cache_key = f"yt:{video_id}"
        audio_waiters_key = f"{cache_key}:audio"

        video_raw = await redis_client.get(cache_key)
        if not video_raw:
            log.error("cache entry %s vanished before audio extraction could run", cache_key)
            waiters = await _pop_waiters(redis_client, audio_waiters_key)
            await _notify_waiters_failure(bot, waiters, "❌ This video is no longer cached, please resend the link.")
            return

        video = YouTubeVideoData.model_validate_json(video_raw)

        lock = Lock(redis_client, f'{audio_waiters_key}:lock', timeout=10 * 60, blocking_timeout=11 * 60)
        async with HeartbeatLock(lock):
            if not video.audio_file_id:
                with tempfile.TemporaryDirectory() as tmp:
                    try:
                        audio_path = await asyncio.to_thread(get_audio_stream, video, Path(tmp))
                    except YouTubeError as e:
                        waiters = await _pop_waiters(redis_client, audio_waiters_key)
                        await _notify_waiters_failure(bot, waiters, f"❌ Couldn't extract audio: {e}")
                        raise

                    video.capture_metadata()

                    for i in range(3):
                        try:
                            media_message = await bot.send_audio(
                                settings.dump_chat_id,
                                types.FSInputFile(audio_path),
                                performer=video.author,
                                title=video.title,
                                duration=video.length,
                            )
                            break
                        except TelegramNetworkError:
                            if i == 2:
                                raise
                            log.warning('failed to send an audio file, retrying in 2 seconds')
                            await asyncio.sleep(2)

                video.audio_file_id = media_message.audio.file_id
                await redis_client.set(cache_key, video.model_dump_json())
                log.info("cached audio for %s", cache_key)

            if await asyncio.to_thread(video.ensure_metadata):
                # promote a pre-metadata entry so the next tap skips YouTube entirely
                await redis_client.set(cache_key, video.model_dump_json())

            waiters = await _pop_waiters(redis_client, audio_waiters_key)
            await _notify_audio_waiters_success(bot, waiters, video)
    finally:
        await redis_client.aclose()


@dramatiq.actor(
    max_retries=2,
    min_backoff=30_000,
    max_backoff=5 * 60_000,
    time_limit=20 * 60_000,
    throws=(YouTubeError,),
    on_retry_exhausted="report_actor_failure",
)
def process_youtube_audio(chat_id: int, video_id: str, reply_to_message_id: int) -> None:
    bot = Bot(token=settings.bot_token)
    try:
        asyncio.run(_process_youtube_audio_async(bot, chat_id, video_id, reply_to_message_id))
    finally:
        asyncio.run(bot.session.close())


@with_chat_action()
async def _process_social_link_async(bot: Bot, chat_id: int, url: str) -> None:
    redis_client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        video = SocialVideoData.model_validate(dict(link=url))

        lock = Lock(redis_client, f'{video.cache_key}:lock', timeout=20 * 60, blocking_timeout=21 * 60)
        async with HeartbeatLock(lock):
            try:
                is_audio, tracks = await asyncio.to_thread(probe_link, url)
            except TransientDownloadError as e:
                await _retry_or_report(
                    bot, redis_client, video.cache_key, e,
                    AudioDownloadError, "❌ Couldn't process this link",
                )
            except AudioDownloadError as e:
                waiters = await _pop_waiters(redis_client, video.cache_key)
                await _notify_waiters_failure(bot, waiters, f"❌ Couldn't process this link: {e}")
                await _alert_if_cookies_stale(redis_client, url, e)
                raise

            if is_audio:
                audio = AudioRequestData(link=url, tracks=tracks)
                page_tracks = audio.page(1)
                await _resolve_cached_tracks(redis_client, page_tracks)
                failed = await handle_audio_page(bot, page_tracks)
                await _save_tracks_to_cache(redis_client, page_tracks)
                await redis_client.set(audio.cache_key, audio.model_dump_json())
                log.info(
                    "cached %s (%d tracks total, page 1 ready, %d failed)",
                    audio.cache_key, len(audio.tracks), failed,
                )

                waiters = await _pop_waiters(redis_client, video.cache_key)
                await _notify_waiters_success(bot, waiters, audio)
                return

            try:
                video = await handle_social_video(bot, video)
            except TransientDownloadError as e:
                await _retry_or_report(
                    bot, redis_client, video.cache_key, e,
                    SocialDownloadError, "❌ Couldn't download this video",
                )
            except SocialDownloadError as e:
                waiters = await _pop_waiters(redis_client, video.cache_key)
                await _notify_waiters_failure(bot, waiters, f"❌ Couldn't download this video: {e}")
                await _alert_if_cookies_stale(redis_client, url, e)
                raise

            await redis_client.set(video.cache_key, video.model_dump_json())
            log.info("cached %s (%s)", video.cache_key, video.origin)

            waiters = await _pop_waiters(redis_client, video.cache_key)
            await _notify_waiters_success(bot, waiters, video)
            for waiter in waiters:
                await on_social_video_sent.send(url, waiter.chat_id, waiter.chat_type, bot, video, True)
    finally:
        await redis_client.aclose()


@dramatiq.actor(
    max_retries=_SOCIAL_MAX_RETRIES,
    min_backoff=30_000,
    max_backoff=5 * 60_000,
    time_limit=25 * 60_000,
    throws=(SocialDownloadError, AudioDownloadError),
    on_retry_exhausted="report_actor_failure",
)
def process_social_link(chat_id: int, url: str) -> None:
    bot = Bot(token=settings.bot_token)
    try:
        asyncio.run(_process_social_link_async(bot, chat_id, url))
    finally:
        asyncio.run(bot.session.close())


@with_chat_action(ChatAction.UPLOAD_VOICE)
async def _process_audio_page_async(bot: Bot, chat_id: int, hash16: str, page: int) -> None:
    redis_client = redis.from_url(str(settings.redis_dsn), decode_responses=True)
    try:
        cache_key = f"da:{hash16}"
        page_key = f"{cache_key}:page:{page}"

        audio_raw = await redis_client.get(cache_key)
        if not audio_raw:
            log.error("cache entry %s vanished before page %d could be processed", cache_key, page)
            waiters = await _pop_waiters(redis_client, page_key)
            await _notify_waiters_failure(bot, waiters, "❌ This playlist is no longer cached, please resend the link.")
            return

        audio = AudioRequestData.model_validate_json(audio_raw)

        lock = Lock(redis_client, f'{page_key}:lock', timeout=20 * 60, blocking_timeout=21 * 60)
        async with HeartbeatLock(lock):
            page_tracks = audio.page(page)
            await _resolve_cached_tracks(redis_client, page_tracks)
            failed = await handle_audio_page(bot, page_tracks)
            await _save_tracks_to_cache(redis_client, page_tracks)
            await redis_client.set(cache_key, audio.model_dump_json())
            log.info("cached %s page %d (%d failed)", cache_key, page, failed)

            waiters = await _pop_waiters(redis_client, page_key)
            await _notify_audio_page_waiters_success(redis_client, bot, waiters, audio, page)
    finally:
        await redis_client.aclose()


@dramatiq.actor(
    max_retries=2,
    min_backoff=30_000,
    max_backoff=5 * 60_000,
    time_limit=30 * 60_000,
    throws=(AudioDownloadError,),
    on_retry_exhausted="report_actor_failure",
)
def process_audio_page(chat_id: int, hash16: str, page: int) -> None:
    bot = Bot(token=settings.bot_token)
    try:
        asyncio.run(_process_audio_page_async(bot, chat_id, hash16, page))
    finally:
        asyncio.run(bot.session.close())
