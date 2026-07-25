from datetime import datetime
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

    # populated on setup
    bot_username: str | None = None
    tz: str | None = None

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.tz or "UTC")

    def now(self) -> datetime:
        return datetime.now(self.timezone)


# noinspection PyArgumentList
settings = Settings()
