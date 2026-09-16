import hashlib
from typing import Literal

from aiogram import Bot, types
from pydantic import BaseModel, Field

# aiogram types `send_media_group`'s argument as an invariant list of this union,
# so building a narrower `list[InputMediaPhoto | InputMediaVideo]` is rejected even
# though every element fits.
type GroupMedia = (
    types.InputMediaAudio
    | types.InputMediaDocument
    | types.InputMediaLivePhoto
    | types.InputMediaPhoto
    | types.InputMediaVideo
)

# Telegram refuses a media group larger than this, so a 13-item carousel goes out
# as two messages rather than one.
_MEDIA_GROUP_LIMIT = 10


class MediaItem(BaseModel):
    """One piece of media already parked in the dump chat.

    Carousel items and the parts of a single oversized video are the same thing
    from here on: an ordered list of file ids, each with its own dimensions.
    Before carousels this was a bare `list[str]` sharing one width/height, which
    only held because every entry was a slice of the same video.
    """

    file_id: str
    kind: Literal["video", "photo"] = "video"
    width: int | None = None
    height: int | None = None

    def as_input_media(self, caption: str | None) -> types.InputMediaPhoto | types.InputMediaVideo:
        if self.kind == "photo":
            return types.InputMediaPhoto(media=self.file_id, caption=caption)
        return types.InputMediaVideo(
            media=self.file_id,
            width=self.width,
            height=self.height,
            caption=caption,
        )


class SocialVideoData(BaseModel):
    link: str
    origin: str = ""
    items: list[MediaItem] = Field(default_factory=list)
    missing: list[int] = Field(default_factory=list)  # carousel positions that failed to download
    video_id: str | None = None
    width: int | None = None
    height: int | None = None
    title: str | None = None

    @property
    def cache_key(self) -> str:
        # `dl2:`, not `dl:` -- entries written under the old prefix hold the
        # pre-carousel `file_ids: list[str]` shape and no longer validate. The
        # bump retires them on their own TTL instead of needing a migration.
        return f"dl2:{hashlib.sha256(self.link.encode()).hexdigest()[:16]}"

    @property
    def caption(self) -> str:
        # title_line = f"{self.title}\n" if self.title else ""
        # return f"{title_line}{self.link}\nby @{settings.bot_username}"
        if not self.missing:
            return self.link
        # A partial carousel has to say so. Silently delivering 12 of 13 looks
        # exactly like a post that only ever had 12 items.
        positions = ", ".join(f"#{pos}" for pos in self.missing)
        return f"{self.link}\n\u26a0\ufe0f Couldn't download {len(self.missing)} item(s): {positions}"

    def _groups(self) -> list[list[MediaItem]]:
        return [self.items[i : i + _MEDIA_GROUP_LIMIT] for i in range(0, len(self.items), _MEDIA_GROUP_LIMIT)]

    async def reply_to(self, message: types.Message) -> None:
        bot = message.bot
        if bot is None:
            raise RuntimeError(f"message {message.message_id} carries no bot instance")
        await self.send_to_chat(bot, message.chat.id, message.message_id)

    async def send_to_chat(self, bot: Bot, chat_id: int, reply_to_message_id: int | None = None) -> None:
        if not self.items:
            raise ValueError(f"nothing to send for {self.link}")

        if len(self.items) == 1:
            item = self.items[0]
            if item.kind == "photo":
                await bot.send_photo(
                    chat_id,
                    photo=item.file_id,
                    caption=self.caption,
                    reply_to_message_id=reply_to_message_id,
                )
            else:
                await bot.send_video(
                    chat_id,
                    video=item.file_id,
                    width=item.width,
                    height=item.height,
                    caption=self.caption,
                    reply_to_message_id=reply_to_message_id,
                )
            return

        # Every group is self-contained: it replies to the link and carries the
        # caption. A later group that did neither read as an unrelated dump of
        # videos -- and on a partial carousel the "couldn't download" warning
        # only ever reached whoever scrolled back to the first message.
        for group in self._groups():
            media: list[GroupMedia] = [
                item.as_input_media(self.caption if i == 0 else None) for i, item in enumerate(group)
            ]
            await bot.send_media_group(chat_id, media, reply_to_message_id=reply_to_message_id)
