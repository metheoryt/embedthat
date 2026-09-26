from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str
    redis_dsn: RedisDsn = Field(
        default="redis://redis", validation_alias=AliasChoices("redis_url")
    )
    loglevel: str = "INFO"

    dump_chat_id: int  # where parts of YouTube videos will be posted to be sent as a media group later
    admin_chat_id: int | None = None

    enable_audio_translation: bool = False
    max_video_resolution: int = 480
    max_playlist_tracks: int = 200

    # Netscape-format cookie jar handed to yt-dlp, unlocking posts that require a
    # logged-in session (Instagram reels, age/sensitivity-gated TikToks). Optional:
    # unset means no cookies, which is exactly the pre-cookie behaviour. Must be
    # writable -- yt-dlp saves the refreshed jar back on close.
    cookies_file: Path | None = None

    # Sent alongside the cookie jar, and only alongside it: the session was
    # created in a real browser, so it should keep presenting as that browser
    # rather than as yt-dlp's built-in Windows-Chrome default. Copy the exact
    # User-Agent of whatever browser exported COOKIES_FILE. Unset = yt-dlp's
    # default, the pre-2026-09-15 behaviour.
    cookies_user_agent: str | None = None

    # Our own telegram-bot-api server in local mode, e.g. http://telegram-bot-api:8081.
    # Unset or empty = Telegram's cloud server, exactly the pre-2026-09-26 behaviour.
    bot_api_url: str | None = None

    # populated on setup
    bot_username: str | None = None
    tz: str | None = None

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.tz or "UTC")

    @property
    def max_upload_size_bytes(self) -> int:
        # Derived from bot_api_url, never set on its own: a rollback that unsets
        # only the URL must also drop the limit, or every large video is sent to
        # the cloud and fails. Decimal MB on the local side on purpose -- it stays
        # under the 2000 MB cap whichever unit Telegram means.
        if self.bot_api_url:
            return 2000 * 1000 * 1000
        return 50 * 1024 * 1024

    def now(self) -> datetime:
        return datetime.now(self.timezone)


# noinspection PyArgumentList
settings = Settings()
