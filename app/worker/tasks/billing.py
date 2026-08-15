"""Задачи биллинга: сказать юзеру про оплату и закрыть истёкшие подписки."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.bot.texts import ru
from app.core.events import track
from app.core.providers.base import EVENT_FAILED, EVENT_PAID
from app.core.services.billing import expire_due
from app.core.services.limits import user_zone
from app.db.models import User
from app.db.repositories.users import telegram_chat_id
from app.db.session import session_scope
from app.logging import bind_request, get_logger

log = get_logger("worker")


def _date(value: str | None, user: User) -> str:
    """Дата окончания в часовом поясе юзера. Пустая строка, если её нет."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return moment.astimezone(user_zone(user)).strftime("%d.%m.%Y")


async def notify_payment(
    ctx: dict[str, Any],
    user_id: str,
    kind: str,
    expires_at: str | None = None,
    renewed: bool = False,
    request_id: str | None = None,
) -> None:
    """Подтверждение оплаты в самом боте.

    Человек платил здесь — здесь и должен увидеть результат, а не только в
    письме от платёжки. Отправляет воркер: у api своего экземпляра бота нет.
    """
    bind_request(request_id, user_id=user_id, job=ctx.get("job_id"))
    bot = ctx["bot"]

    async with session_scope() as session:
        user = await session.get(User, uuid.UUID(user_id))
        if user is None:
            log.error("оплата пришла от неизвестного пользователя", user_id=user_id)
            return
        chat_id = await telegram_chat_id(session, user.id)
        if chat_id is None:
            log.error("некуда сообщить об оплате: нет telegram-профиля", user_id=user_id)
            return

        if kind == EVENT_PAID:
            date = _date(expires_at, user)
            text = (
                ru.PAYMENT_RENEWED.format(date=date) if renewed else ru.PAYMENT_OK.format(date=date)
            )
        elif kind == EVENT_FAILED:
            text = ru.PAYMENT_FAILED
        else:
            # Отмена автопродления: доступ до конца оплаченного срока остаётся,
            # и пугать человека сообщением «подписка отменена» не за что.
            log.info("событие оплаты без сообщения юзеру", событие=kind, user_id=user_id)
            return

        try:
            await bot.send_message(chat_id, text)
        except Exception:  # noqa: BLE001  бот мог быть заблокирован юзером
            log.warning("сообщение об оплате не доставлено", user_id=user_id, chat_id=chat_id)
            return
        await track(session, "payment_notified", user_id=user.id, событие=kind)
        log.info("юзер уведомлён об оплате", user_id=user_id, событие=kind, продление=renewed)


async def expire_subscriptions(ctx: dict[str, Any]) -> None:
    """Перевести истёкшие подписки в expired и сказать об этом их владельцам.

    Раз в час, а не раз в сутки: подписка кончается в момент, а не в полночь,
    и человек должен узнать об этом от бота, а не по внезапно появившемуся
    счётчику под ответом.
    """
    bot = ctx["bot"]
    async with session_scope() as session:
        expired = await expire_due(session)
        for user_id in expired:
            chat_id = await telegram_chat_id(session, user_id)
            if chat_id is None:
                continue
            try:
                await bot.send_message(chat_id, ru.SUBSCRIPTION_ENDED)
            except Exception:  # noqa: BLE001
                log.warning("сообщение об окончании подписки не доставлено", user_id=str(user_id))
    if expired:
        log.info("подписки закрыты по сроку", количество=len(expired))
