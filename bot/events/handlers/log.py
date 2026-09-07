import logging

from aiogram.types import Message

from bot.enum import LinkOrigin

from ..signals import on_link_received, signal_handler

log = logging.getLogger(__name__)


@signal_handler(on_link_received)
async def log_link(message: Message, origin: LinkOrigin) -> None:
    # Every field but `origin` is attacker-controlled: a display name or a chat
    # title may contain newlines, so `%s` lets anyone forge whole log lines (one
    # user's name in a real sample was a `<prompt>...</prompt>` injection aimed at
    # whoever reads these logs). `%r` escapes the newline and quotes the value, so
    # a hostile name can only ever be one line's argument.
    log.info(
        "%s link in %r from %r @%r: %r",
        origin,
        message.chat.title if message.chat.type != "private" else "private chat",
        message.from_user.full_name,
        message.from_user.username,
        message.text,
    )
