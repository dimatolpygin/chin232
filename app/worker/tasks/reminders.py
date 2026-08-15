"""Напоминание тем, кто упёрся в лимит и попросил позвать его завтра."""

from __future__ import annotations

from typing import Any

from app.bot.texts import ru
from app.core.events import track
from app.core.services.limits import take_due_reminders
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger("worker")


async def send_limit_reminders(ctx: dict[str, Any]) -> None:
    """Разослать напоминания тем, у кого местный день уже сменился.

    Задача крутится каждый час, а не раз в сутки по расписанию контейнера:
    время у контейнера UTC, а полночь у пользователя своя. Час — достаточная
    точность для «напомню завтра» и не требует лишних знаний о часовых поясах
    в самом расписании.
    """
    bot = ctx["bot"]
    queue = ctx.get("redis")
    if queue is None:
        return

    async with session_scope() as session:
        due = await take_due_reminders(queue, session)
        for user, chat_id in due:
            if not chat_id:
                continue
            try:
                await bot.send_message(chat_id, ru.REMIND_READY)
            except Exception:  # noqa: BLE001  бот мог быть заблокирован юзером
                log.warning("напоминание не доставлено", user_id=str(user.id), chat_id=chat_id)
                continue
            await track(session, "limit_reminder_sent", user_id=user.id)
            log.info("напоминание об обновлении лимита отправлено", user_id=str(user.id))
    if due:
        log.info("напоминания разосланы", получателей=len(due))
