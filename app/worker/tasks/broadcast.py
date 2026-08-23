"""Рассылка. Живёт в воркере, потому что бот всё это время обязан отвечать.

Телеграм разрешает около тридцати сообщений в секунду разным адресатам, и
превышение наказывается не отказом, а временной блокировкой всего бота —
включая ответы живым людям. Поэтому темп занижен с запасом, а на просьбу
подождать (`retry_after`) рассылка честно останавливается.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.bot.texts import ru
from app.core.events import track
from app.core.services.admin import SEGMENT_TITLES, audience
from app.db.session import session_scope
from app.logging import bind_request, get_logger

log = get_logger("worker")

# 20 в секунду вместо разрешённых 30: остаток оставлен живым разговорам, они
# идут через того же бота и в ту же квоту.
RATE_PER_SEC = 20
PAUSE_SEC = 1 / RATE_PER_SEC

# Сколько раз повторяем после просьбы подождать. Один повтор — это норма при
# всплеске; если и он не прошёл, адресат уходит в ошибки, а рассылка едет дальше.
RETRIES = 1


def _elapsed(seconds: float) -> str:
    минут, секунд = divmod(int(seconds), 60)
    return f"{минут} мин {секунд} с" if минут else f"{секунд} с"


async def _send_one(bot, chat_id: int, text: str) -> str:
    """Отправить одному. Возвращает исход: `sent`, `blocked` или `failed`."""
    for попытка in range(RETRIES + 1):
        try:
            await bot.send_message(chat_id, text, disable_web_page_preview=True)
            return "sent"
        except TelegramRetryAfter as exc:
            # Единственная ошибка, которую нельзя игнорировать: продолжить в том
            # же темпе — значит получить блокировку всего бота.
            log.warning(
                "телеграм просит подождать, рассылка приостановлена",
                секунд=exc.retry_after,
                попытка=попытка + 1,
            )
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            # Заблокировал бота или удалил аккаунт. Это не ошибка рассылки.
            return "blocked"
        except Exception as exc:  # noqa: BLE001  один адресат не роняет рассылку
            log.warning("сообщение не доставлено", chat_id=chat_id, ошибка=repr(exc))
            return "failed"
    return "failed"


async def run_broadcast(
    ctx: dict[str, Any],
    segment: str,
    text: str,
    admin_chat_id: int,
    request_id: str | None = None,
) -> dict[str, int]:
    """Разослать текст сегменту и отчитаться админу.

    Список адресатов берём одним запросом и сессию тут же закрываем: рассылка
    длится минуты, и держать соединение с базой открытым всё это время незачем.
    """
    bind_request(request_id, job=ctx.get("job_id"))
    bot = ctx["bot"]
    started = time.monotonic()

    async with session_scope() as session:
        люди = await audience(session, segment)

    log.info("рассылка начата", сегмент=segment, адресатов=len(люди), знаков=len(text))

    итоги = {"sent": 0, "blocked": 0, "failed": 0}
    for _user_id, chat_id in люди:
        итоги[await _send_one(bot, chat_id, text)] += 1
        await asyncio.sleep(PAUSE_SEC)

    заняло = time.monotonic() - started
    отчёт = ru.ADMIN_BROADCAST_REPORT.format(
        segment=SEGMENT_TITLES.get(segment, segment),
        sent=итоги["sent"],
        total=len(люди),
        blocked=итоги["blocked"],
        failed=итоги["failed"],
        elapsed=_elapsed(заняло),
    )
    try:
        await bot.send_message(admin_chat_id, отчёт)
    except Exception as exc:  # noqa: BLE001  отчёт не должен ронять задачу
        log.warning("отчёт о рассылке не доставлен", ошибка=repr(exc))

    async with session_scope() as session:
        await track(
            session,
            "broadcast_finished",
            сегмент=segment,
            адресатов=len(люди),
            доставлено=итоги["sent"],
            заблокировали=итоги["blocked"],
            ошибок=итоги["failed"],
        )
    log.info(
        "рассылка закончена",
        сегмент=segment,
        адресатов=len(люди),
        доставлено=итоги["sent"],
        заблокировали=итоги["blocked"],
        ошибок=итоги["failed"],
        секунд=round(заняло, 1),
    )
    return итоги
