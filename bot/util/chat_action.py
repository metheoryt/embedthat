import asyncio
import logging

from aiogram import Bot
from aiogram.enums import ChatAction

log = logging.getLogger(__name__)

chat_action_tasks: dict[int, asyncio.Task[None]] = {}

async def send_chat_action_periodically(bot: Bot, chat_id: int, action: ChatAction) -> asyncio.Task[None]:
    # Check if there's already an action being sent for this chat_id
    if chat_id in chat_action_tasks:
        # If there's an active task, cancel it
        log.debug('cancelling %d chat action task due to the new task', chat_id)
        chat_action_tasks[chat_id].cancel()

    async def action_task() -> None:
        try:
            while True:
                log.debug('sending %d chat action %s', chat_id, action)
                await bot.send_chat_action(chat_id=chat_id, action=action)
                await asyncio.sleep(4)  # Telegram allows one action every ~5 seconds
        except asyncio.CancelledError:
            log.debug('%d chat action task canceled', chat_id)
            pass  # Graceful exit on cancellation

    # Start and return the new task
    new_task = asyncio.create_task(action_task())
    new_task.add_done_callback(lambda t: chat_action_tasks.pop(chat_id, None))
    chat_action_tasks[chat_id] = new_task
    log.debug('new %d chat action task started', chat_id)

    return new_task
