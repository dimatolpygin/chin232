"""Воркер arq. Тяжёлое (голосовой круг, рассылки) уходит сюда, а не в хендлер."""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from arq.connections import RedisSettings

from app.config import get_settings
from app.db.session import dispose_engine
from app.logging import bind_request, clear_request, configure_logging, get_logger
from app.worker.tasks import greet_user, process_voice_round

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
log = get_logger("worker")


async def ping(ctx: dict[str, Any]) -> str:
    """Заглушка-задача: подтверждает, что очередь жива."""
    log.info("задача ping выполнена", job_id=ctx.get("job_id"))
    return "pong"


async def on_startup(ctx: dict[str, Any]) -> None:
    # Свой экземпляр Bot: воркер сам скачивает голосовые и сам отправляет ответ.
    ctx["bot"] = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    log.info("воркер запущен", окружение=settings.env, очередь=settings.redis_prefix + "arq")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    bot = ctx.get("bot")
    if bot is not None:
        await bot.session.close()
    await dispose_engine()
    log.info("воркер остановлен")


async def on_job_start(ctx: dict[str, Any]) -> None:
    bind_request(request_id=None, job=ctx.get("job_id"))
    log.info("задача взята в работу", попытка=ctx.get("job_try"), job_id=ctx.get("job_id"))


async def on_job_end(ctx: dict[str, Any]) -> None:
    log.info("задача завершена", job_id=ctx.get("job_id"))
    clear_request()


class WorkerSettings:
    functions = [ping, process_voice_round, greet_user]
    on_startup = on_startup
    on_shutdown = on_shutdown
    on_job_start = on_job_start
    on_job_end = on_job_end
    queue_name = settings.redis_prefix + "arq"
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 120
    # Круг не ретраим вслепую: повтор стоит денег на всех четырёх сервисах,
    # а юзеру уже отправлено понятное сообщение об ошибке.
    max_tries = 1
