from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from bot.config import settings

# How long an upload may take end to end against our own server, which replies
# only after it has pushed the file on to Telegram. Worker-only: polling adds the
# session timeout to every getUpdates wait, so main.py must keep the default.
UPLOAD_TIMEOUT = 30 * 60


def make_bot(token: str | None = None, *, uploads: bool = False, **kwargs: Any) -> Bot:
    """The only place a `Bot` is built. The API server is per-session, so a `Bot`
    constructed anywhere else silently keeps talking to the cloud -- with a token
    that is logged out there once we switch."""
    session = None
    if settings.bot_api_url:
        api = TelegramAPIServer.from_base(settings.bot_api_url, is_local=True)
        if uploads:
            session = AiohttpSession(api=api, timeout=UPLOAD_TIMEOUT)
        else:
            session = AiohttpSession(api=api)
    return Bot(token or settings.bot_token, session=session, **kwargs)
