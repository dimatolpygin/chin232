"""Задачи биллинга: сказать юзеру про оплату и закрыть истёкшие подписки."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.bot.texts import ru
from app.core.events import track
from app.core.providers.base import EVENT_FAILED, EVENT_PAID, EVENT_REFUNDED
from app.core.services.billing import due_for_reminder, expire_due, get_plan, mark_reminded
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


async def _autorenews(session, plan_code: str) -> bool:
    """Продлевается ли тариф сам. Незнакомый тариф считаем подпиской."""
    plan = await get_plan(session, plan_code)
    if plan is None or plan.autorenew is None:
        return True
    return plan.autorenew


async def notify_payment(
    ctx: dict[str, Any],
    user_id: str,
    kind: str,
    expires_at: str | None = None,
    renewed: bool = False,
    request_id: str | None = None,
    plan_code: str | None = None,
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
            if renewed:
                text = ru.PAYMENT_RENEWED.format(date=date)
            elif plan_code and not (await _autorenews(session, plan_code)):
                # Разовая оплата: обещать автопродление, которого не будет,
                # значит подставить человека под молчаливую потерю доступа.
                text = ru.PAYMENT_OK_ONCE.format(date=date)
            else:
                text = ru.PAYMENT_OK.format(date=date)
        elif kind == EVENT_FAILED:
            text = ru.PAYMENT_FAILED
        elif kind == EVENT_REFUNDED:
            text = ru.PAYMENT_REFUNDED
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


async def remind_expiring(ctx: dict[str, Any]) -> None:
    """Напомнить об окончании доступа, купленного разово.

    Только тарифам без автопродления: у подписки картой деньги спишутся сами,
    и напоминание было бы поводом отменить, а не продлить. Отметка о том, что
    напомнили, стоит в самой подписке, иначе сообщение уходило бы каждый час.
    """
    bot = ctx["bot"]
    sent = 0
    async with session_scope() as session:
        for subscription in await due_for_reminder(session):
            user = await session.get(User, subscription.user_id)
            if user is None:
                continue
            chat_id = await telegram_chat_id(session, user.id)
            if chat_id is None:
                continue
            date = subscription.expires_at.astimezone(user_zone(user)).strftime("%d.%m.%Y")
            try:
                await bot.send_message(chat_id, ru.SUBSCRIPTION_ENDING_SOON.format(date=date))
            except Exception:  # noqa: BLE001  бот мог быть заблокирован юзером
                log.warning("напоминание об окончании не доставлено", user_id=str(user.id))
                # Отмечаем всё равно: недоставленное сообщение не станет
                # доставленным через час, а долбить заблокировавшего незачем.
                await mark_reminded(session, subscription)
                continue
            await mark_reminded(session, subscription)
            await track(session, "subscription_ending_notified", user_id=user.id)
            sent += 1
    if sent:
        log.info("напоминания об окончании доступа отправлены", количество=sent)
