import asyncio
import logging
import re

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import ErrorEvent, Message

from .config import settings
from .dispatcher import router
from .enum import LinkOrigin
from .events import on_link_received, on_social_video_sent, on_yt_video_sent
from .util.audio.pager import redeliver_page
from .util.audio.schema import AudioRequestData
from .util.chat import is_group_chat
from .util.redis import redis_client
from .util.social.schema import SocialVideoData
from .util.stats import build_stats_report
from .util.youtube.enum import TargetLang
from .util.youtube.schema import YouTubeVideoData
from .worker.actors import (
    process_audio_page,
    process_social_link,
    process_youtube_audio,
    process_youtube_link,
)
from .worker.waiters import Waiter, register_waiter

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+")
_YOUTUBE_URL_RE = re.compile(r"https?://((www|m)\.)?youtube\.com/|https?://youtu\.be/")

# Hosts that are definitionally not a video or audio post, skipped before a job
# is ever queued. `embed_social` reacts to any message containing a URL, so in a
# group chat an ordinary pasted wiki or repo link used to cost a full yt-dlp
# attempt -- 14 of the 44 dead-lettered jobs on 2026-09-15 were links like these,
# and the Instagram ones among them spent the burner account's session on a
# fetch that could never have returned a video.
#
# A DENYLIST, not an allowlist, on purpose. /start promises "and more", and
# yt-dlp supports thousands of sites; an allowlist that forgets one silently
# stops working with no error the user can see. Letting a junk host through
# costs one wasted attempt -- exactly what happens today -- so the cheap-to-be-
# wrong side is the right one. Add to this only for hosts that can never carry
# media, never for a platform that merely fails often.
_NOT_MEDIA_HOST_RE = re.compile(
    r"^https?://([^/@]*\.)?("
    r"github\.com|gitlab\.com|"
    r"wikipedia\.org|wikimedia\.org|"
    r"t\.me|telegra\.ph|telegraph\.co\.uk|"
    r"google\.com|docs\.google\.com|"
    r"stackoverflow\.com|"
    r"pypi\.org|npmjs\.com"
    r")([/?#]|$)",
    re.IGNORECASE,
)

_YOUTUBE_WAITERS_TTL = 3 * 60 * 60  # generous vs. worst-case retry budget (~2.5h)
_SOCIAL_WAITERS_TTL = 90 * 60  # ~1.5h


@router.error()
async def error_handler(event: ErrorEvent) -> None:
    message = event.update.message
    log.critical(
        "Unhandled error while processing update (message text: %r)",
        message.text if message else None,
        exc_info=event.exception,
    )


@router.message(CommandStart())
async def start(message: types.Message) -> None:
    log.info(
        "/start in %s %r (chat id=%s)",
        message.chat.type,
        message.chat.title or "private chat",
        message.chat.id,
    )
    await message.reply(
        "Send a link and I'll reply with a nice embedding or a video.\n\n"
        "📹 Video: YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit, and more\n"
        "🎵 Audio: SoundCloud, Bandcamp, Mixcloud, Audiomack, and more"
    )


@router.channel_post(CommandStart())
async def start_channel(message: types.Message) -> None:
    # Channel posts never reach @router.message, so a channel has no other way to
    # reveal its id. No reply: posting a welcome text into a channel is noise.
    log.info("/start in channel %r (chat id=%s)", message.chat.title, message.chat.id)


@router.message(Command("stats"))
async def cmd_stats(message: types.Message) -> None:
    if not settings.admin_chat_id or message.chat.id != settings.admin_chat_id:
        return
    await message.reply(await build_stats_report())


@router.message(
    F.text.regexp(r"^https://(((www|m)\.)?youtube\.com/(watch|shorts/)|youtu\.be/)")
)
async def embed_youtube_videos(message: types.Message) -> None:
    await on_link_received.send(message, LinkOrigin.YOUTUBE)
    link = message.text.split()[0]  # as regex states, we expect the first element in the text to be a link

    if settings.enable_audio_translation:
        log.info('user lang: %s', message.from_user.language_code)
        try:
            target_lang = TargetLang(message.from_user.language_code)
        except ValueError:
            target_lang = TargetLang.ORIGINAL
            log.info('no translation will be performed')
    else:
        target_lang = TargetLang.ORIGINAL

    video = YouTubeVideoData.model_validate(dict(link=link, target_lang=target_lang))

    if video_raw := await redis_client.get(video.cache_key):
        cached = YouTubeVideoData.model_validate_json(video_raw)
        log.info("cache hit for %s", video.cache_key)
        if await asyncio.to_thread(cached.ensure_metadata):
            # promote a pre-metadata entry so the next hit skips YouTube entirely
            await redis_client.set(video.cache_key, cached.model_dump_json())
        try:
            await cached.reply_to(message)
        except TelegramBadRequest:
            log.info("cached telegram file ids failed to be posted, removing from cache")
            await redis_client.delete(video.cache_key)
        else:
            await on_yt_video_sent.send(link, message.chat.id, message.chat.type, message.bot, cached, False)
            return

    log.info("cache miss for %s, registering waiter", video.cache_key)
    waiter = Waiter(
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        reply_to_message_id=message.message_id,
    )
    is_first = await register_waiter(redis_client, video.cache_key, waiter, _YOUTUBE_WAITERS_TTL)
    if is_first:
        process_youtube_link.send(message.chat.id, link, target_lang.value)


@router.callback_query(F.data.startswith("aud:"))
async def get_audio(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not isinstance(callback.message, types.Message):
        return

    video_id = callback.data.removeprefix("aud:")
    cache_key = f"yt:{video_id}"

    video_raw = await redis_client.get(cache_key)
    if not video_raw:
        if not is_group_chat(callback.message.chat.id):
            await callback.message.reply("❌ This video is no longer cached, please resend the link.")
        return

    # remove the button right away so a repeat tap can't queue/duplicate a delivery
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    video = YouTubeVideoData.model_validate_json(video_raw)
    if video.audio_file_id:
        if await asyncio.to_thread(video.ensure_metadata):
            # promote a pre-metadata entry so the next tap skips YouTube entirely
            await redis_client.set(cache_key, video.model_dump_json())
        await callback.message.answer_audio(
            video.audio_file_id,
            performer=video.author,
            title=video.title,
            duration=video.length,
        )
        return

    log.info("cache miss for audio %s, registering waiter", cache_key)
    waiter = Waiter(
        chat_id=callback.message.chat.id,
        chat_type=callback.message.chat.type,
        reply_to_message_id=callback.message.message_id,
    )
    is_first = await register_waiter(redis_client, f"{cache_key}:audio", waiter, _YOUTUBE_WAITERS_TTL)
    if is_first:
        process_youtube_audio.send(callback.message.chat.id, video_id, callback.message.message_id)


@router.callback_query(F.data == "apg:noop")
async def noop_page_indicator(callback: types.CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("apg:"))
async def get_audio_page(callback: types.CallbackQuery) -> None:
    await callback.answer()
    if not isinstance(callback.message, types.Message):
        return

    _, hash16, page_str, root_str = callback.data.split(":")
    page = int(page_str)
    root_message_id = int(root_str)
    cache_key = f"da:{hash16}"

    # remove the pager buttons right away so a repeat tap can't queue/duplicate a delivery
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    audio_raw = await redis_client.get(cache_key)
    if not audio_raw:
        if not is_group_chat(callback.message.chat.id):
            await callback.message.reply("❌ This playlist is no longer cached, please resend the link.")
        return

    audio = AudioRequestData.model_validate_json(audio_raw)
    page_tracks = audio.page(page)
    if all(t.file_id for t in page_tracks):
        await redeliver_page(
            redis_client, callback.message.bot, callback.message.chat.id, root_message_id, audio, page,
        )
        return

    log.info("cache miss for %s page %d, registering waiter", cache_key, page)
    page_key = f"{cache_key}:page:{page}"
    waiter = Waiter(
        chat_id=callback.message.chat.id,
        chat_type=callback.message.chat.type,
        reply_to_message_id=root_message_id,
    )
    is_first = await register_waiter(redis_client, page_key, waiter, _SOCIAL_WAITERS_TTL)
    if is_first:
        process_audio_page.send(callback.message.chat.id, hash16, page)


async def _process_social_url(message: Message, url: str) -> None:
    audio = AudioRequestData(link=url)
    if audio_raw := await redis_client.get(audio.cache_key):
        cached_audio = AudioRequestData.model_validate_json(audio_raw)
        log.info("cache hit (audio) for %s", audio.cache_key)
        await redeliver_page(redis_client, message.bot, message.chat.id, message.message_id, cached_audio, page=1)
        return

    video = SocialVideoData.model_validate(dict(link=url))

    if video_raw := await redis_client.get(video.cache_key):
        cached = SocialVideoData.model_validate_json(video_raw)
        log.info("cache hit for %s", video.cache_key)
        try:
            await cached.reply_to(message)
        except TelegramBadRequest:
            log.info("cached file ids invalid, clearing cache for %s", video.cache_key)
            await redis_client.delete(video.cache_key)
        else:
            await on_social_video_sent.send(url, message.chat.id, message.chat.type, message.bot, cached, False)
            return

    log.info("cache miss for %s, registering waiter", video.cache_key)
    waiter = Waiter(
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        reply_to_message_id=message.message_id,
    )
    is_first = await register_waiter(redis_client, video.cache_key, waiter, _SOCIAL_WAITERS_TTL)
    if is_first:
        process_social_link.send(message.chat.id, url)


@router.message(F.text.regexp(r"https?://"))
async def embed_social(message: types.Message) -> None:
    urls = [
        u for u in _URL_RE.findall(message.text)
        if not _YOUTUBE_URL_RE.match(u) and not _NOT_MEDIA_HOST_RE.match(u)
    ]
    if not urls:
        return

    await on_link_received.send(message, LinkOrigin.SOCIAL)

    for url in urls:
        await _process_social_url(message, url)
