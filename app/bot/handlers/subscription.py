"""Раздел «Подписка»: что даёт, сколько стоит, где оплатить."""

from __future__ import annotations

import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.subscription import (
    SUB_PAY,
    SUB_PREFIX,
    offer_keyboard,
    parse_subscription_action,
    payment_keyboard,
)
from app.bot.render import esc
from app.bot.texts import ru
from app.config import get_settings
from app.core.events import track
from app.core.providers.base import ProviderError
from app.core.services.billing import (
    DEFAULT_PLAN,
    BillingError,
    active_subscription,
    get_plan,
    payable_plans,
    start_payment,
)
from app.core.services.limits import KIND_CHECK, KIND_MESSAGE, get_limits, limit_for, user_zone
from app.db.models import Plan, User
from app.db.models.billing import SUB_CANCELLED
from app.logging import get_logger

router = Router(name="subscription")
log = get_logger("bot")

# Нарочно нестрогая: наше дело — отсечь опечатку и случайную реплику разговора,
# а настоящую доставляемость проверит платёжка, когда пришлёт на адрес счёт.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

# Сколько ждём почту после вопроса. Дольше — и адресом станет реплика разговора,
# сказанная через полчаса совсем по другому поводу.
EMAIL_WAIT_SEC = 900

# Метка в ключе ожидания: адрес меняют командой, а не в процессе оплаты.
# После ввода показываем раздел с кнопками, а не выставляем счёт: человек
# просил сменить почту, а не платить.
EMAIL_ONLY = "-"


def email_key(user: User) -> str:
    return get_settings().redis_key("await_email", str(user.id))


def money(value: Decimal | float | int) -> str:
    """Цена без хвоста копеек, если их нет: «590 ₽», а не «590.00 ₽»."""
    number = Decimal(str(value))
    return f"{number:f}".rstrip("0").rstrip(".") if number % 1 else str(int(number))


def currency_sign(code: str) -> str:
    return ru.CURRENCY_SIGNS.get(code, code)


def _date(value, user: User) -> str:
    return value.astimezone(user_zone(user)).strftime("%d.%m.%Y")


async def show_subscription(message: Message, session: AsyncSession, user: User) -> None:
    """Экран раздела: у подписчика — до какой даты, у остальных — предложение."""
    current = await active_subscription(session, user)
    if current is not None:
        template = (
            ru.SUBSCRIPTION_CANCELLED
            if current.status == SUB_CANCELLED
            else (ru.SUBSCRIPTION_ACTIVE)
        )
        await message.answer(template.format(date=_date(current.expires_at, user)))
        return

    plans = await payable_plans(session)
    if not plans:
        await message.answer(ru.SUBSCRIPTION_NOT_READY)
        log.error("нет ни одного тарифа с оффером платёжки", user_id=str(user.id))
        return

    # Первый в списке — подписка картой, по ней и показываем цену в тексте.
    main = plans[0]
    limits = await get_limits(session)
    messages_limit, _ = limit_for(user, limits, KIND_MESSAGE)
    checks_limit, _ = limit_for(user, limits, KIND_CHECK)
    # Развилку про СБП показываем, только когда выбирать действительно есть из
    # чего: пока заказчик не завёл разовый товар, тариф в списке один.
    template = ru.SUBSCRIPTION_OFFER if len(plans) > 1 else ru.SUBSCRIPTION_OFFER_SINGLE
    await message.answer(
        template.format(
            title=main.title,
            free_messages=messages_limit,
            free_checks=checks_limit,
            price=money(main.price),
            currency=currency_sign(main.currency),
            days=main.duration_days,
        ),
        reply_markup=offer_keyboard(
            [(plan, money(plan.price), currency_sign(plan.currency)) for plan in plans]
        ),
    )


async def _ask_email(message: Message, user: User, queue, plan: Plan) -> None:
    """Спросить почту и запомнить, за какой тариф человек нажал.

    Выбор способа оплаты сделан кнопкой ДО вопроса про почту, и потерять его
    нельзя: иначе выбравший СБП получит счёт на карту.
    """
    await queue.set(email_key(user), plan.code, ex=EMAIL_WAIT_SEC)
    await message.answer(ru.SUBSCRIPTION_ASK_EMAIL)
    log.info("запрошена почта для оплаты", user_id=str(user.id), тариф=plan.code)


# Платёжка отвечает на плохой адрес именно так. Продавец, например, не может
# купить собственный продукт на почту своего же аккаунта — проверено вживую.
EMAIL_REJECTED_MARK = "Incorrect email"


async def _send_invoice(
    message: Message, session: AsyncSession, user: User, plan: Plan, queue=None
) -> None:
    """Выставить счёт и отдать юзеру ссылку.

    Сбой платёжки здесь — не авария бота: разговор продолжает работать, а юзеру
    нужен понятный текст, а не «что-то пошло не так».
    """
    try:
        started = await start_payment(session, user, plan)
    except BillingError as exc:
        log.error("счёт не выставлен", user_id=str(user.id), причина=str(exc))
        await message.answer(ru.SUBSCRIPTION_NOT_READY)
        return
    except ProviderError as exc:
        log.error(
            "платёжка не выставила счёт",
            user_id=str(user.id),
            http_код=exc.status_code,
            тело_ответа=(exc.body or "")[:1000],
        )
        if EMAIL_REJECTED_MARK in (exc.body or "") and queue is not None:
            # Адрес не подошёл: стираем его и спрашиваем заново, иначе человек
            # будет жать «оплатить» по кругу и получать одну и ту же ошибку.
            user.email = None
            await session.flush()
            await queue.set(email_key(user), plan.code, ex=EMAIL_WAIT_SEC)
            await message.answer(ru.SUBSCRIPTION_EMAIL_REJECTED)
            await track(session, "email_rejected", user_id=user.id, тариф=plan.code)
            return
        await message.answer(ru.SUBSCRIPTION_ERROR)
        return

    note = ru.LINK_NOTE_CARD if plan.autorenew else ru.LINK_NOTE_SBP.format(days=plan.duration_days)
    await message.answer(
        ru.SUBSCRIPTION_LINK.format(
            price=money(plan.price),
            currency=currency_sign(plan.currency),
            note=note,
        ),
        reply_markup=payment_keyboard(started.payment_url),
    )


async def _plan_or_default(session: AsyncSession, code: str) -> Plan | None:
    """Тариф из кнопки, иначе тариф по умолчанию.

    Пустой или незнакомый код — это старая кнопка из давней переписки или
    ключ, оставшийся в redis с прошлой версии. Отказывать человеку в оплате
    из-за этого незачем: молча берём тариф по умолчанию.
    """
    plan = await get_plan(session, code) if code else None
    return plan or await get_plan(session, DEFAULT_PLAN)


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, session: AsyncSession, user: User) -> None:
    await show_subscription(message, session, user)
    await track(session, "subscription_opened", user_id=user.id, источник="команда")


@router.message(Command("email"))
async def cmd_email(message: Message, session: AsyncSession, user: User, queue) -> None:
    """Сменить почту для оплаты.

    Адрес сохраняется один раз и дальше не спрашивается, поэтому опечатка или
    неподходящий ящик иначе чинились бы только через нас. Тариф берём по
    умолчанию: способ оплаты человек выберет кнопкой после ввода адреса.
    """
    previous = user.email
    user.email = None
    await session.flush()
    await queue.set(email_key(user), EMAIL_ONLY, ex=EMAIL_WAIT_SEC)
    await message.answer(ru.SUBSCRIPTION_ASK_EMAIL)
    await track(session, "email_reset", user_id=user.id)
    log.info("почта сброшена по команде", user_id=str(user.id), была_задана=bool(previous))


@router.callback_query(F.data.startswith(f"{SUB_PREFIX}:"))
async def on_subscription_action(
    callback: CallbackQuery, session: AsyncSession, user: User, queue
) -> None:
    parsed = parse_subscription_action(callback.data or "")
    if parsed is None or parsed[0] != SUB_PAY:
        await callback.answer()
        return
    # Часики гасим до работы: выставление счёта — сетевой вызов, а запоздалый
    # ответ Telegram отвергает с «query is too old». Этим уже обожглись на
    # кнопке «Текст» на этапе 2.
    await callback.answer()

    plan = await _plan_or_default(session, parsed[1])
    if plan is None or not plan.offer_id:
        # Тарифа нет или оффер не заведён в кабинете платёжки: ссылка на оплату
        # получилась бы битой.
        await callback.message.answer(ru.SUBSCRIPTION_NOT_READY)
        log.error("оплата невозможна: нет тарифа или оффера", тариф=parsed[1] or DEFAULT_PLAN)
        return

    await track(session, "subscribe_pay_clicked", user_id=user.id, тариф=plan.code)
    if not user.email:
        await _ask_email(callback.message, user, queue, plan)
        return
    await _send_invoice(callback.message, session, user, plan, queue)


@router.message(F.text & ~F.text.startswith("/"))
async def on_email(message: Message, session: AsyncSession, user: User, queue) -> None:
    """Почта в ответ на вопрос. Всё остальное уходит дальше, в разговор.

    Роутер стоит раньше разговорного, поэтому пропускаем чужое явно: без
    `SkipHandler` любая реплика застревала бы здесь и до круга не доезжала.
    """
    # В ключе лежит код тарифа, выбранный кнопкой до вопроса про почту.
    chosen = await queue.get(email_key(user))
    if not chosen:
        raise SkipHandler
    chosen = chosen.decode() if isinstance(chosen, bytes) else str(chosen)

    email = (message.text or "").strip()
    if not EMAIL_RE.match(email) or len(email) > 320:
        await message.answer(ru.SUBSCRIPTION_BAD_EMAIL)
        log.info("введённая почта не принята", user_id=str(user.id))
        return

    await queue.delete(email_key(user))
    user.email = email
    await session.flush()
    await track(session, "email_saved", user_id=user.id)
    log.info("почта покупателя сохранена", user_id=str(user.id))
    await message.answer(ru.SUBSCRIPTION_EMAIL_SAVED.format(email=esc(email)))

    if chosen == EMAIL_ONLY:
        # Пришли из /email: показываем раздел, платить человек не просил.
        await show_subscription(message, session, user)
        return

    plan = await _plan_or_default(session, chosen)
    if plan is None or not plan.offer_id:
        await message.answer(ru.SUBSCRIPTION_NOT_READY)
        return
    await _send_invoice(message, session, user, plan, queue)
