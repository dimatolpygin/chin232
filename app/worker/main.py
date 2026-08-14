"""Воркер arq. Тяжёлое (голосовой круг, рассылки) уходит сюда, а не в хендлер."""

from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.db.session import dispose_engine
from app.logging import bind_request, clear_request, configure_logging, get_logger

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
log = get_logger("worker")


async def ping(ctx: dict[str, Any]) -> str:
    """Заглушка-задача: подтверждает, что очередь жива. Этап 1 добавит голосовой круг."""
    log.info("задача ping выполнена", job_id=ctx.get("job_id"))
    return "pong"


async def on_startup(_ctx: dict[str, Any]) -> None:
    log.info("воркер запущен", окружение=settings.env, очередь=settings.redis_prefix + "arq")


async def on_shutdown(_ctx: dict[str, Any]) -> None:
    await dispose_engine()
    log.info("воркер остановлен")


async def on_job_start(ctx: dict[str, Any]) -> None:
    bind_request(request_id=None, job=ctx.get("job_id"))
    log.info("задача взята в работу", задача=ctx.get("job_try"), job_id=ctx.get("job_id"))


async def on_job_end(ctx: dict[str, Any]) -> None:
    log.info("задача завершена", job_id=ctx.get("job_id"))
    clear_request()


class WorkerSettings:
    functions = [ping]
    on_startup = on_startup
    on_shutdown = on_shutdown
    on_job_start = on_job_start
    on_job_end = on_job_end
    queue_name = settings.redis_prefix + "arq"
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 120
