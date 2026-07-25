import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

from aiogram import Bot
from aiogram.enums import ChatAction

from bot.util.chat_action import send_chat_action_periodically

R = TypeVar("R")

AsyncFn = Callable[..., Awaitable[R]]


def with_chat_action(
    action: ChatAction = ChatAction.UPLOAD_VIDEO,
) -> Callable[[AsyncFn[R]], AsyncFn[R]]:
    def decorator(func: AsyncFn[R]) -> AsyncFn[R]:
        @wraps(func)
        async def wrapper(bot: Bot, chat_id: int, *args: Any, **kwargs: Any) -> R:
            action_task = await send_chat_action_periodically(bot, chat_id, action)
            try:
                return await func(bot, chat_id, *args, **kwargs)
            finally:
                action_task.cancel()
                try:
                    await action_task
                except asyncio.CancelledError:
                    pass
        return wrapper
    return decorator
