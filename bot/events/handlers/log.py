import logging

from aiogram.types import Message

from bot.enum import LinkOrigin

from ..signals import on_link_received, signal_handler

log = logging.getLogger(__name__)


@signal_handler(on_link_received)
async def log_link(message: Message, origin: LinkOrigin) -> None:
    log.info(
        "%s link in %s from %s @%s: %s",
        origin,
        message.chat.title if message.chat.type != "private" else "private chat",
        message.from_user.full_name,
        message.from_user.username,
        message.text,
    )
