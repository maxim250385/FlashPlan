import asyncio
import logging
import os
import random
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand, MenuButtonCommands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from modules.database import init_db
from modules.handlers import router
from modules.brain_handlers import brain_router
from modules.scheduler import check_reminders, check_short_reminders
from modules.ai_handler import ALL_PROXIES


async def _create_bot() -> Bot:
    """Перебирает все прокси в случайном порядке, возвращает первый рабочий бот."""
    proxies = ALL_PROXIES.copy()
    random.shuffle(proxies)

    for proxy in proxies:
        session = None
        try:
            session = AiohttpSession(proxy=proxy)
            bot = Bot(token=os.environ["BOT_TOKEN"], session=session)
            await bot.get_me()
            logger.info(f"Telegram подключён через прокси {proxy}")
            return bot
        except Exception as e:
            logger.warning(f"Прокси {proxy} не работает для Telegram: {e}")
            if session is not None:
                await session.close()

    raise RuntimeError("Ни один из 10 прокси не смог подключиться к Telegram API.")


async def main():
    await init_db()

    bot = await _create_bot()
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(brain_router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить / перезапустить бота"),
        BotCommand(command="help", description="Список команд"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, trigger='interval', minutes=1, args=[bot])
    scheduler.add_job(check_short_reminders, trigger='interval', minutes=1, args=[bot])
    scheduler.start()

    logger.info("Бот запущен. Ожидание сообщений...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
