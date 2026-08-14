"""Воркер arq. Тяжёлое (голосовой круг, рассылки) уходит сюда, а не в хендлер."""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from arq import cron
from arq.connections import RedisSettings
from structlog.contextvars import bind_contextvars

from app.config import get_settings
from app.core.providers.http import close_client, warmup
from app.db.session import dispose_engine
from app.logging import clear_request, configure_logging, get_logger
from app.worker.tasks import greet_user, process_voice_round

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
log = get_logger("worker")


async def ping(ctx: dict[str, Any]) -> str:
    """Заглушка-задача: подтверждает, что очередь жива."""
    log.info("задача ping выполнена", job_id=ctx.get("job_id"))
    return "pong"


async def keep_connections_warm(ctx: dict[str, Any]) -> None:
    """Держит TLS-соединения живыми между разговорами.

    Простаивающее соединение закрывается, и следующий круг снова платит за
    рукопожатие — те самые лишние восемь секунд на первом сообщении.
    """
    await warmup()


async def on_startup(ctx: dict[str, Any]) -> None:
    # Свой экземпляр Bot: воркер сам скачивает голосовые и сам отправляет ответ.
    ctx["bot"] = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await warmup()
    log.info("воркер запущен", окружение=settings.env, очередь=settings.redis_prefix + "arq")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    bot = ctx.get("bot")
    if bot is not None:
        await bot.session.close()
    await close_client()
    await dispose_engine()
    log.info("воркер остановлен")


async def on_job_start(ctx: dict[str, Any]) -> None:
    # request_id здесь не выдумываем: настоящий приходит в аргументах задачи из
    # бота, и задача привяжет его сама. Свой случайный id рвал бы цепочку —
    # обёртка жила бы под одним идентификатором, а сама работа под другим.
    bind_contextvars(job=ctx.get("job_id"))
    log.info("задача взята в работу", попытка=ctx.get("job_try"), job_id=ctx.get("job_id"))


async def on_job_end(ctx: dict[str, Any]) -> None:
    log.info("задача завершена", job_id=ctx.get("job_id"))
    clear_request()


class WorkerSettings:
    functions = [ping, process_voice_round, greet_user]
    # Каждые четыре минуты: раньше, чем сервер успеет закрыть простаивающее
    # соединение по своему таймауту.
    cron_jobs = [cron(keep_connections_warm, minute=set(range(0, 60, 4)), run_at_startup=False)]
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
