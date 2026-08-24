"""Сообщения админам от самого бота: то, о чём человек обязан узнать без спроса.

Сюда попадает только то, что требует решения: копия базы не собралась, сервер
упёрся в потолок. Не «всё хорошо» — на такие сообщения перестают смотреть
через неделю, и вместе с ними перестают смотреть на важные.
"""

from __future__ import annotations

from typing import Any

from app.core.services.admin import admin_ids
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger("worker")


async def notify_admins(bot: Any, text: str) -> int:
    """Разослать текст всем админам. Возвращает, скольким дошло."""
    async with session_scope() as session:
        получатели = sorted(await admin_ids(session))

    if not получатели:
        log.warning("некому сообщить: список админов пуст", сообщение=text[:200])
        return 0

    дошло = 0
    for chat_id in получатели:
        try:
            await bot.send_message(chat_id, text)
            дошло += 1
        except Exception as exc:  # noqa: BLE001  один заблокировавший бота админ
            # не должен лишать сообщения остальных
            log.warning("админу не доставлено", chat_id=chat_id, ошибка=repr(exc))
    log.info("админы оповещены", получателей=дошло, из_них_всего=len(получатели))
    return дошло
