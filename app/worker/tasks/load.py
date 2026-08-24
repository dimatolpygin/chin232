"""Сторож нагрузки: сам сообщает, когда сервер перестал справляться.

Метрика, в которую надо не забыть заглянуть, не работает. Поэтому цифры
считаются каждый час, и, если машина упёрлась в потолок, бот пишет админу сам.

Ровно один раз в сутки: сервер меняют не за минуту, и десять одинаковых
сообщений подряд приведут только к тому, что их начнут пролистывать.
"""

from __future__ import annotations

from typing import Any

from app.bot.texts import ru
from app.config import get_settings
from app.core.events import track
from app.core.services.load import BUDGET_SEC, load_report
from app.db.session import session_scope
from app.logging import get_logger
from app.worker.notify import notify_admins

log = get_logger("worker")

# Окно, по которому судим. Час — слишком дёрганно (одна ночная задача сдвинет
# картину), сутки — слишком поздно: вечерний час пик утонет в спокойной ночи.
WATCH_HOURS = 6

# Сколько молчать после отправленного предупреждения.
QUIET_SEC = 24 * 3600


async def watch_load(ctx: dict[str, Any]) -> None:
    """Посчитать нагрузку и, если дело плохо, сказать об этом человеку."""
    settings = get_settings()
    queue = ctx.get("redis")
    bot = ctx.get("bot")

    async with session_scope() as session:
        отчёт = await load_report(session, hours=WATCH_HOURS, queue=queue)
        if not отчёт.alarming:
            return
        await track(
            session,
            "load_ceiling",
            кругов=отчёт.rounds,
            медленных=отчёт.slow,
            ожидание_p95=отчёт.wait_p95,
            очередь=отчёт.queue_depth,
        )

    if bot is None or queue is None:
        return

    # Замок на сутки ставим ДО отправки: лучше не предупредить второй раз, чем
    # завалить админа одинаковыми сообщениями, если отправка частично упала.
    ключ = settings.redis_key("load", "alerted")
    if not await queue.set(ключ, "1", nx=True, ex=QUIET_SEC):
        log.info(
            "потолок нагрузки: админам уже сообщали сегодня",
            кругов=отчёт.rounds,
            ожидание_p95=отчёт.wait_p95,
        )
        return

    await notify_admins(
        bot,
        ru.LOAD_ALERT.format(
            hours=WATCH_HOURS,
            rounds=отчёт.rounds,
            budget=f"{BUDGET_SEC:.0f}",
            slow=отчёт.slow,
            wait_p95=отчёт.wait_p95,
        ),
    )
    log.warning(
        "сервер упёрся в потолок, админы предупреждены",
        кругов=отчёт.rounds,
        медленных=отчёт.slow,
        ожидание_p95=отчёт.wait_p95,
        очередь=отчёт.queue_depth,
        ядер=отчёт.cores,
    )
