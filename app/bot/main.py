"""Точка входа бота."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

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
        await bot.session.close()
        await dispose_engine()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
