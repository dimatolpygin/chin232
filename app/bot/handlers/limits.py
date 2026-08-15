"""Стена дневного лимита и кнопки под ней."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.limits import (
    LIMIT_PREFIX,
    LIMIT_REMIND,
    LIMIT_SUBSCRIBE,
    limit_keyboard,
    parse_limit_action,
)
from app.bot.texts import ru
from app.core.events import track
from app.core.services.limits import KIND_CHECK, Quota, ask_remind, consume, peek
from app.db.models import User
from app.logging import get_logger

router = Router(name="limits")
log = get_logger("bot")


def wall_text(quota: Quota) -> str:
    template = ru.LIMIT_REACHED_CHECKS if quota.kind == KIND_CHECK else ru.LIMIT_REACHED
    return template.format(limit=quota.limit)


async def show_wall(message: Message, quota: Quota) -> None:
    """Экран исчерпанного лимита. Разбор произношения кнопки оплаты не получает.

    Кончившиеся разборы — не конец работы: разговор продолжается бесплатно, и
    предлагать за них деньги в тот же момент значит торговать паникой.
    """
    await message.answer(
        wall_text(quota),
        reply_markup=None if quota.kind == KIND_CHECK else limit_keyboard(),
    )


async def spend(
    message: Message, session: AsyncSession, user: User, queue, kind: str
) -> Quota | None:
    """Списать действие. None — лимит исчерпан, юзеру уже показана стена.

    Вызывать строго ДО постановки задачи в очередь: за стеной не должно быть ни
    одного платного вызова.
    """
    quota = await consume(queue, session, user, kind)
    if quota.allowed:
        return quota
    await show_wall(message, quota)
    return None


async def check_left(message: Message, session: AsyncSession, user: User, queue, kind: str) -> bool:
    """Проверить остаток, ничего не списывая.

    Нужна там, где само нажатие уже стоит денег (озвучка эталона, подбор
    подсказки), а списание произойдёт позже или не произойдёт вовсе.
    """
    quota = await peek(queue, session, user, kind)
    if quota.allowed:
        return True
    log.info(
        "действие отклонено лимитом",
        user_id=str(user.id),
        счётчик=kind,
        израсходовано=quota.used,
        лимит=quota.limit,
    )
    await track(session, "limit_blocked", user_id=user.id, счётчик=kind, лимит=quota.limit)
    await show_wall(message, quota)
    return False


@router.callback_query(F.data.startswith(f"{LIMIT_PREFIX}:"))
async def on_limit_action(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
) -> None:
    action = parse_limit_action(callback.data or "")
    if action is None:
        await callback.answer()
        return
    await callback.answer()

    if action == LIMIT_SUBSCRIBE:
        # Оплата подключается этапом 5. До тех пор кнопка честно говорит, что
        # именно происходит, а не молчит.
        await callback.message.answer(ru.SUBSCRIBE_SOON)
        await track(session, "subscribe_clicked", user_id=user.id, источник="limit_wall")
        log.info("нажата кнопка подписки", user_id=str(user.id))
        return

    if action == LIMIT_REMIND:
        await ask_remind(queue, user, callback.message.chat.id)
        await callback.message.answer(ru.REMIND_OK)
        await track(session, "remind_requested", user_id=user.id)
