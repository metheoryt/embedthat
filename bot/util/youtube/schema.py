import logging
from functools import cached_property

from aiogram import Bot, types
from pydantic import BaseModel, Field
from pytubefix import YouTube

from .enum import SourceLang, TargetLang
from .exc import translates_youtube_errors

log = logging.getLogger(__name__)


class YouTubeVideoData(BaseModel):
    link: str
    target_lang: TargetLang = TargetLang.ORIGINAL

    #
    # populated after the video was processed
    #
    file_ids: list[str] = Field(default_factory=list)

    # real video size
    width: int | None = None
    height: int | None = None

    # Detected video language.
    # None if target_lang=ORIGINAL (detection was not performed)
    #   or detected language is not supported.
    source_lang: SourceLang | None = None

    # whether the video was translated to the target_lang (if target lang is not ORIGINAL)
    translated_lang: TargetLang | None = None

    # Telegram file_id of the audio-only extraction, populated on first "🎵 Get audio" request
    audio_file_id: str | None = None

    # Snapshot of YouTube's own metadata, taken while the video was still downloadable.
    # Reading yt.title/.author/.length is a ~1s live query against YouTube, so without
    # this every redelivery from cache re-fetches what we already know -- and worse,
    # raises once the video goes private/age-gated, breaking redelivery of file_ids we
    # already hold. None on pre-metadata entries -- see ensure_metadata.
    title: str | None = None
    author: str | None = None
    length: int | None = None

    @cached_property
    def yt(self) -> YouTube:
        # Deliberately no `client=` argument. Which InnerTube client YouTube still
        # serves changes without notice, and pytubefix's default is the one upstream
        # keeps current: 10.10.1 defaulted to ANDROID_VR, which started answering
        # every request with BotDetection on 2026-09-01 and took the whole YouTube
        # path down until 11.1.0 (default VISION_OS) restored it. Pinning a client
        # here would have frozen us on the broken one -- and the obvious pin, "WEB",
        # is the worst of them: it resolves streams fine but the CDN then serves a
        # 31-byte body for every range request unless a PO token rides along.
        return YouTube(self.link)

    @translates_youtube_errors
    def capture_metadata(self) -> None:
        """Snapshot title/author/length off `yt`.

        Effectively free once `yt` has fetched its streams (pytubefix memoizes
        vid_info), so call it on the processing path, where we know the video was
        reachable a moment ago -- never on the redelivery path.

        Reads all three before assigning any: on a cold `yt` these are live queries
        that can fail individually (a restricted video may still answer for .title
        but not .length), and a half-populated model would look complete to
        ensure_metadata and never be retried.
        """
        title, author, length = self.yt.title, self.yt.author, self.yt.length
        self.title, self.author, self.length = title, author, length

    def ensure_metadata(self) -> bool:
        """Best-effort backfill for entries cached before these fields existed.

        Returns True if it actually fetched, so the caller can re-save the entry and
        make this a one-off. Blocking -- call via asyncio.to_thread. Deliberately
        never raises: the files are already in the dump chat, so a video that has
        since been restricted should still redeliver, just captioned without its
        title. New entries make this a no-op, which is what keeps redelivery off the
        network entirely.
        """
        if self.title is not None:
            return False
        try:
            self.capture_metadata()
        except Exception as e:
            # `link` and not `cache_key`: the latter builds a YouTube object and can
            # raise, which would defeat the whole point of this handler
            log.warning("could not backfill metadata for %s: %r", self.link, e)
            return False
        return True

    @property
    def cache_key(self) -> str:
        return f"yt:{self.yt.video_id}"

    @property
    def caption(self) -> str:
        # title is empty only for pre-metadata entries YouTube won't describe any
        # more; drop the line entirely rather than lead the caption with a blank one
        title = self.title or ''
        if self.translated_lang and self.translated_lang != TargetLang.ORIGINAL:
            title = f"{title} [{self.translated_lang} audio]".lstrip()
        return f"{title}\n{self.link}\n" if title else f"{self.link}\n"

    @property
    def media_group(self) -> list[types.InputMediaVideo]:
        the_group = [
            types.InputMediaVideo(
                media=file_id,
                width=self.width,
                height=self.height,
                caption=self.caption if i == 0 else None,
            )
            for i, file_id in enumerate(self.file_ids)
        ]
        return the_group

    @property
    def audio_button_markup(self) -> types.InlineKeyboardMarkup:
        return types.InlineKeyboardMarkup(
            inline_keyboard=[[
                types.InlineKeyboardButton(text="🎵 Get audio", callback_data=f"aud:{self.yt.video_id}")
            ]]
        )

    async def send_to_chat(self, bot: Bot, chat_id: int, reply_to_message_id: int | None = None) -> None:
        if len(self.file_ids) > 1:
            # send_media_group doesn't support reply_markup, so the button has to ride a follow-up message
            await bot.send_media_group(chat_id, self.media_group, reply_to_message_id=reply_to_message_id)
            await bot.send_message(chat_id, "🎵 Want just the audio?", reply_markup=self.audio_button_markup)
        else:
            await bot.send_video(
                chat_id,
                video=self.file_ids[0],
                width=self.width,
                height=self.height,
                caption=self.caption,
                reply_to_message_id=reply_to_message_id,
                reply_markup=self.audio_button_markup,
            )

    async def reply_to(self, message: types.Message) -> None:
        if len(self.file_ids) > 1:
            await message.reply_media_group(self.media_group)
            await message.reply("🎵 Want just the audio?", reply_markup=self.audio_button_markup)
        else:
            await message.reply_video(
                video=self.file_ids[0],
                width=self.width,
                height=self.height,
                caption=self.caption,
                reply_markup=self.audio_button_markup,
            )
