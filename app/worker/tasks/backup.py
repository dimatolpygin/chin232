"""Ночная копия базы в стороннее хранилище.

Задача воркера, а не cron на хосте: скрипт, лежащий только на сервере, живёт
ровно до переустановки этого сервера — а копии нужны как раз тогда, когда
сервера не стало. Здесь же она едет вместе с кодом и поднимается сама.

Локальные дампы в `/opt/backup` при этом остаются: восстановиться из соседней
папки быстрее, чем из сети. Это два разных уровня, а не дубликат.
"""

from __future__ import annotations

from typing import Any

from app.bot.texts import ru
from app.config import get_settings
from app.core.events import track
from app.core.services.backup import run_backup
from app.db.session import session_scope
from app.logging import get_logger
from app.worker.notify import notify_admins

log = get_logger("worker")


async def backup_database(ctx: dict[str, Any]) -> None:
    """Снять копию, отправить в хранилище, убрать старьё.

    Молча падать нельзя: бэкап, о поломке которого никто не знает, — это не
    бэкап, а вера в него. Поэтому любой сбой уходит админам в телеграм.
    """
    settings = get_settings()
    bot = ctx.get("bot")

    if not settings.s3_endpoint or not settings.s3_bucket:
        # Не ошибка, а незаконченная настройка: на машине разработки хранилища
        # нет и быть не должно.
        log.info("копия базы пропущена: хранилище не настроено")
        return

    try:
        итог = await run_backup(settings)
    except Exception as exc:  # noqa: BLE001  падение задачи не должно ронять воркер
        log.exception("копия базы не собралась", ошибка=repr(exc))
        async with session_scope() as session:
            await track(session, "backup_failed", ошибка=repr(exc)[:500])
        if bot is not None:
            await notify_admins(bot, ru.BACKUP_FAILED.format(error=str(exc)[:300]))
        return

    async with session_scope() as session:
        await track(
            session,
            "backup_done",
            файл=итог.name,
            байт=итог.size,
            секунд=итог.seconds,
            удалено=len(итог.deleted),
            всего=итог.kept,
        )
    log.info(
        "ночная копия готова",
        файл=итог.name,
        мегабайт=round(итог.size / 1024 / 1024, 2),
        всего_копий=итог.kept,
    )
