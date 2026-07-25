import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from bot.config import settings
from bot.dispatcher import dp, router
from bot.events import freeze_signals
from bot.util.telegram_log_handler import install_admin_alert_handler


async def main() -> None:
    from bot import handlers  # noqa

    the_bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp.include_router(router)
    me = await the_bot.get_me()

    # populate some data
    settings.bot_username = me.username

    # freeze signals before starting the polling (non-frozen signals unable to send signals)
    freeze_signals()
    await dp.start_polling(the_bot)


def setup() -> None:
    load_dotenv()
    logging.basicConfig(
        level=getattr(logging, settings.loglevel),
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    )
    # disable logs for non-handled events
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    # logging.getLogger("pytube").setLevel(logging.DEBUG)
    # logging.getLogger("pytubefix").setLevel(logging.DEBUG)
    install_admin_alert_handler()


if __name__ == "__main__":
    setup()
    asyncio.run(main())
