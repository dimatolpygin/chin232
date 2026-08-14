"""Точка входа бота."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from arq import create_pool
from arq.connections import RedisSettings

from app.bot.handlers import build_router
from app.bot.middlewares.logging import LoggingMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.config import get_settings
from app.db.session import dispose_engine
from app.logging import configure_logging, get_logger


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log = get_logger("bot")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    # Пул очереди кладём в workflow-данные: хендлеры получают его аргументом
    # `queue` и ставят тяжёлое в arq, не выполняя в обработчике апдейта.
    queue = await create_pool(
        RedisSettings.from_dsn(settings.redis_url),
        default_queue_name=settings.redis_prefix + "arq",
    )
    dp["queue"] = queue
    # Порядок важен: сначала лог и request_id, потом пользователь в контекст.
    dp.update.outer_middleware(LoggingMiddleware())
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(build_router())

    me = await bot.get_me()
    log.info("бот запускается", бот=f"@{me.username}", окружение=settings.env)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        log.info("бот остановлен")
        await queue.aclose()
        await bot.session.close()
        await dispose_engine()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
