"""Подписка и деньги.

Правило этого файла: **деньги идут через базу, а не через память процесса**.
Вебхук может прийти дважды, прийти раньше ответа платёжки на создание счёта и
прийти по контракту, которого мы не создавали (автопродление). Всё это должно
приводить к одному результату — подписка продлена ровно один раз.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import track
from app.core.providers.base import (
    EVENT_CANCELLED,
    EVENT_FAILED,
    EVENT_PAID,
    EVENT_REFUNDED,
    PaymentEvent,
)
from app.core.providers.registry import get_payments
from app.db.models import Payment, Plan, Subscription, User
from app.db.models.billing import (
    PAY_CANCELLED,
    PAY_COMPLETED,
    PAY_FAILED,
    PAY_PENDING,
    PAY_REFUNDED,
    SUB_ACTIVE,
    SUB_CANCELLED,
    SUB_EXPIRED,
)
from app.logging import get_logger

log = get_logger("billing")

DEFAULT_PLAN = "monthly"


class BillingError(RuntimeError):
    """Оплату сейчас начать нельзя. Текст пригоден для показа юзеру."""


@dataclass(slots=True)
class Started:
    """Счёт выставлен, юзеру есть куда нажимать."""

    payment_url: str
    external_id: str
    plan: Plan


@dataclass(slots=True)
class Applied:
    """Что вебхук сделал с подпиской. `duplicate` — не сделал ничего."""

    kind: str
    duplicate: bool = False
    # Продление, а не первая покупка: юзеру об этом пишется другой текст.
    renewed: bool = False
    user_id: uuid.UUID | None = None
    expires_at: datetime | None = None
    amount: float | None = None
    currency: str | None = None
    # По коду тарифа воркер понимает, обещать автопродление или напоминание.
    plan_code: str | None = None


async def get_plan(session: AsyncSession, code: str = DEFAULT_PLAN) -> Plan | None:
    plan = await session.get(Plan, code)
    return plan if plan is not None and plan.active else None


async def payable_plans(session: AsyncSession) -> list[Plan]:
    """Тарифы, которые можно показать юзеру: включены и заведены у платёжки.

    Тариф без `offer_id` — это тариф, по которому ссылка на оплату получится
    битой, поэтому он не показывается вовсе. Порядок фиксированный: сначала
    подписка с автопродлением, потом разовые покупки.
    """
    result = await session.execute(
        select(Plan)
        .where(Plan.active.is_(True), Plan.offer_id.is_not(None))
        .order_by(Plan.autorenew.desc(), Plan.price)
    )
    return list(result.scalars().all())


async def active_subscription(session: AsyncSession, user: User) -> Subscription | None:
    """Действующая подписка или None.

    Смотрим на дату, а не только на статус: статус переставляет почасовая
    задача, и между её запусками просроченная подписка ещё числится активной.
    Пускать по ней в платные сервисы нельзя.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_([SUB_ACTIVE, SUB_CANCELLED]),
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def has_active_subscription(session: AsyncSession, user: User) -> bool:
    return await active_subscription(session, user) is not None


async def start_payment(session: AsyncSession, user: User, plan: Plan) -> Started:
    """Выставить счёт и запомнить его до прихода вебхука.

    Платёж записывается сразу, со статусом `pending`: если человек оплатит, а
    вебхук придёт раньше, чем мы успеем что-то сохранить, деньги окажутся
    привязаны к контракту, о котором мы не знаем.
    """
    if not plan.offer_id:
        raise BillingError("у тарифа не задан оффер платёжки")
    if not user.email:
        raise BillingError("у пользователя нет почты")

    provider = get_payments()
    invoice = await provider.create_invoice(
        offer_id=plan.offer_id,
        email=user.email,
        currency=plan.currency,
        periodicity=plan.periodicity,
        # Способ оплаты живёт в тарифе, а не в коде: одна ссылка на оплату —
        # один способ, платёжка вшивает его прямо в виджет.
        paymentProvider=plan.payment_provider,
        paymentMethod=plan.payment_method,
    )

    if invoice.amount is not None and abs(float(invoice.amount) - float(plan.price)) > 0.01:
        # Цену показываем свою, а списывает платёжка свою. Расхождение — это
        # обманутый юзер, поэтому оно обязано быть видно в логах сразу.
        log.warning(
            "цена тарифа расходится с ценой оффера платёжки",
            тариф=plan.code,
            цена_в_базе=float(plan.price),
            цена_у_платёжки=float(invoice.amount),
        )

    session.add(
        Payment(
            user_id=user.id,
            provider=provider.name,
            external_id=invoice.external_id,
            plan_code=plan.code,
            amount=invoice.amount if invoice.amount is not None else plan.price,
            currency=invoice.currency or plan.currency,
            status=PAY_PENDING,
            raw=invoice.raw,
        )
    )
    await track(
        session,
        "payment_started",
        user_id=user.id,
        тариф=plan.code,
        контракт=invoice.external_id,
        сумма=float(invoice.amount) if invoice.amount is not None else float(plan.price),
        валюта=invoice.currency or plan.currency,
    )
    log.info(
        "счёт выставлен",
        user_id=str(user.id),
        тариф=plan.code,
        контракт=invoice.external_id,
        сумма=float(invoice.amount) if invoice.amount is not None else float(plan.price),
        валюта=invoice.currency or plan.currency,
        ссылка_есть=bool(invoice.payment_url),
    )
    if not invoice.payment_url:
        raise BillingError("платёжка не вернула ссылку на оплату")
    return Started(payment_url=invoice.payment_url, external_id=invoice.external_id, plan=plan)


async def _find_user_id(session: AsyncSession, event: PaymentEvent) -> uuid.UUID | None:
    """Чей это платёж.

    Порядок попыток от надёжного к запасному: свой же контракт, родительский
    контракт (продление приходит с новым идентификатором), почта покупателя.
    """
    for external in (event.external_id, event.parent_external_id):
        if not external:
            continue
        found = await session.scalar(
            select(Payment.user_id).where(Payment.external_id == external).limit(1)
        )
        if found:
            return found
        found = await session.scalar(
            select(Subscription.user_id).where(Subscription.external_id == external).limit(1)
        )
        if found:
            return found
    if event.email:
        return await session.scalar(
            select(User.id).where(User.email == event.email).order_by(User.created_at).limit(1)
        )
    return None


async def _record_payment(
    session: AsyncSession,
    user_id: uuid.UUID,
    provider: str,
    event: PaymentEvent,
    status: str,
    plan_code: str | None,
) -> bool:
    """Записать движение денег. False — такое уже записано, это повтор.

    Идемпотентность стоит на уникальной паре «провайдер + контракт» и на
    условии в `DO UPDATE`: уже оплаченный контракт второй раз не обновится, а
    значит и подписку не продлит. Проверка и запись — один запрос, поэтому две
    одновременные доставки вебхука не разъедутся.
    """
    row = await session.execute(
        text(
            "INSERT INTO payments (id, user_id, provider, external_id, parent_external_id, "
            "plan_code, amount, currency, status, raw, paid_at) "
            "VALUES (gen_random_uuid(), :user_id, :provider, :external_id, :parent, :plan_code, "
            ":amount, :currency, :status, CAST(:raw AS jsonb), :paid_at) "
            "ON CONFLICT (provider, external_id) DO UPDATE SET "
            "status = EXCLUDED.status, raw = EXCLUDED.raw, paid_at = EXCLUDED.paid_at, "
            "amount = COALESCE(EXCLUDED.amount, payments.amount), "
            "currency = COALESCE(EXCLUDED.currency, payments.currency), "
            "parent_external_id = COALESCE(EXCLUDED.parent_external_id, "
            "payments.parent_external_id), updated_at = now() "
            "WHERE payments.status <> :completed "
            "RETURNING id"
        ),
        {
            "user_id": user_id,
            "provider": provider,
            "external_id": event.external_id,
            "parent": event.parent_external_id,
            "plan_code": plan_code,
            "amount": event.amount,
            "currency": event.currency,
            "status": status,
            "raw": _dump(event.raw),
            "paid_at": datetime.now(UTC) if status == PAY_COMPLETED else None,
            "completed": PAY_COMPLETED,
        },
    )
    return row.first() is not None


def _dump(raw: object) -> str:
    """Тело вебхука в jsonb. Не разобравшееся сохраняем строкой, а не теряем."""
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"_raw": str(raw)}, ensure_ascii=False)


async def _extend(
    session: AsyncSession, user_id: uuid.UUID, plan: Plan, event: PaymentEvent
) -> datetime:
    """Продлить или выдать подписку. Возвращает новую дату окончания.

    Считаем от текущей даты окончания, если она ещё не прошла: человек, который
    продлился заранее, не должен терять оплаченный остаток.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    current = result.scalar_one_or_none()
    parent = event.parent_external_id or event.external_id

    if current is not None and current.expires_at > now and current.status != SUB_EXPIRED:
        current.expires_at = current.expires_at + timedelta(days=plan.duration_days)
        current.status = SUB_ACTIVE
        current.external_id = current.external_id or parent
        return current.expires_at

    expires = now + timedelta(days=plan.duration_days)
    session.add(
        Subscription(
            user_id=user_id,
            plan_code=plan.code,
            status=SUB_ACTIVE,
            started_at=now,
            expires_at=expires,
            source="lavatop",
            external_id=parent,
        )
    )
    return expires


async def apply_event(session: AsyncSession, event: PaymentEvent, provider: str) -> Applied:
    """Применить вебхук платёжки. Единственная точка, где деньги становятся подпиской."""
    if not event.external_id:
        log.error("вебхук без идентификатора контракта", событие=event.kind)
        return Applied(kind=event.kind, duplicate=False)

    user_id = await _find_user_id(session, event)
    if user_id is None:
        # Возвращать платёжке ошибку смысла нет: она будет слать этот вебхук
        # девятнадцать раз, а найти юзера от этого не получится.
        log.error(
            "платёж не привязался к пользователю",
            контракт=event.external_id,
            родительский_контракт=event.parent_external_id,
            почта=event.email,
            событие=event.kind,
        )
        await track(session, "payment_orphan", контракт=event.external_id, событие=event.kind)
        return Applied(kind=event.kind)

    plan_code = await session.scalar(
        select(Payment.plan_code)
        .where(Payment.external_id == (event.parent_external_id or event.external_id))
        .limit(1)
    )
    plan = await get_plan(session, plan_code or DEFAULT_PLAN) or await get_plan(session)

    if event.kind == EVENT_PAID:
        if plan is None:
            log.error("оплата пришла, а тарифа в базе нет", контракт=event.external_id)
            return Applied(kind=event.kind, user_id=user_id)
        first = await _record_payment(session, user_id, provider, event, PAY_COMPLETED, plan.code)
        if not first:
            log.warning(
                "повторный вебхук об оплате: подписка не продлевается",
                user_id=str(user_id),
                контракт=event.external_id,
            )
            await track(
                session,
                "payment_duplicate",
                user_id=user_id,
                контракт=event.external_id,
            )
            return Applied(kind=event.kind, duplicate=True, user_id=user_id)

        expires = await _extend(session, user_id, plan, event)
        await track(
            session,
            "payment_succeeded",
            user_id=user_id,
            контракт=event.external_id,
            тариф=plan.code,
            сумма=event.amount,
            валюта=event.currency,
            продление=event.recurring,
            действует_до=expires.isoformat(),
        )
        log.info(
            "деньги получены, подписка активна",
            user_id=str(user_id),
            контракт=event.external_id,
            сумма=event.amount,
            валюта=event.currency,
            продление=event.recurring,
            действует_до=expires.isoformat(),
        )
        return Applied(
            kind=event.kind,
            renewed=event.recurring,
            user_id=user_id,
            expires_at=expires,
            amount=event.amount,
            currency=event.currency,
            plan_code=plan.code,
        )

    if event.kind == EVENT_FAILED:
        await _record_payment(
            session, user_id, provider, event, PAY_FAILED, plan.code if plan else None
        )
        await track(
            session,
            "payment_failed",
            user_id=user_id,
            контракт=event.external_id,
            причина=event.error,
        )
        log.warning(
            "оплата не прошла",
            user_id=str(user_id),
            контракт=event.external_id,
            причина=event.error,
        )
        return Applied(kind=event.kind, user_id=user_id)

    if event.kind == EVENT_REFUNDED:
        # Возврат и спор с банком: денег больше нет, значит нет и доступа.
        # Платёж правим отдельным UPDATE, а не через идемпотентный INSERT:
        # тот нарочно не трогает уже оплаченные строки, а здесь надо тронуть.
        await session.execute(
            text(
                "UPDATE payments SET status = :refunded, raw = CAST(:raw AS jsonb), "
                "updated_at = now() WHERE provider = :provider AND external_id = :external"
            ),
            {
                "refunded": PAY_REFUNDED,
                "raw": _dump(event.raw),
                "provider": provider,
                "external": event.external_id,
            },
        )
        closed = await session.execute(
            text(
                "UPDATE subscriptions SET status = :expired, expires_at = now(), "
                "updated_at = now() WHERE user_id = :user_id AND expires_at > now() "
                "RETURNING id"
            ),
            {"expired": SUB_EXPIRED, "user_id": user_id},
        )
        revoked = len(closed.fetchall())
        await track(
            session,
            "payment_refunded",
            user_id=user_id,
            контракт=event.external_id,
            закрыто_подписок=revoked,
        )
        log.warning(
            "возврат денег: доступ закрыт",
            user_id=str(user_id),
            контракт=event.external_id,
            закрыто_подписок=revoked,
            сумма=event.amount,
        )
        return Applied(kind=event.kind, user_id=user_id)

    if event.kind == EVENT_CANCELLED:
        # Отмена — это отказ от следующего списания, а не изъятие оплаченного.
        # Дату окончания не трогаем: человек доиспользует то, за что заплатил.
        await session.execute(
            text(
                "UPDATE subscriptions SET status = :cancelled, updated_at = now() "
                "WHERE user_id = :user_id AND status = :active"
            ),
            {"cancelled": SUB_CANCELLED, "active": SUB_ACTIVE, "user_id": user_id},
        )
        await _record_payment(
            session, user_id, provider, event, PAY_CANCELLED, plan.code if plan else None
        )
        await track(session, "subscription_cancelled", user_id=user_id, контракт=event.external_id)
        log.info(
            "подписка отменена, доступ сохраняется до конца оплаченного срока",
            user_id=str(user_id),
            контракт=event.external_id,
            до=event.expires_at,
        )
        return Applied(kind=event.kind, user_id=user_id)

    log.warning("неизвестное событие платёжки", событие=event.raw.get("eventType"))
    return Applied(kind=event.kind, user_id=user_id)


async def due_for_reminder(session: AsyncSession, within_hours: int = 48) -> list[Subscription]:
    """Кому пора напомнить, что доступ заканчивается.

    Только тарифы без автопродления: по подписке картой деньги спишутся сами,
    и напоминание было бы поводом отменить, а не продлить. Отменённую подписку
    берём тоже: автопродления у неё уже не будет, а доступ кончится.
    """
    horizon = datetime.now(UTC) + timedelta(hours=within_hours)
    result = await session.execute(
        select(Subscription)
        .join(Plan, Plan.code == Subscription.plan_code)
        .where(
            Plan.autorenew.is_(False),
            Subscription.status.in_([SUB_ACTIVE, SUB_CANCELLED]),
            Subscription.reminded_at.is_(None),
            Subscription.expires_at > datetime.now(UTC),
            Subscription.expires_at <= horizon,
        )
        .order_by(Subscription.expires_at)
    )
    return list(result.scalars().all())


async def mark_reminded(session: AsyncSession, subscription: Subscription) -> None:
    """Отметить, что напоминание ушло. Иначе оно уйдёт снова через час."""
    subscription.reminded_at = datetime.now(UTC)
    await session.flush()


async def expire_due(session: AsyncSession) -> list[uuid.UUID]:
    """Перевести истёкшие подписки в expired. Возвращает, кого задело.

    Доступ и без этого закрывается по дате, задача нужна для честной
    статистики и для того, чтобы юзеру было что сказать.
    """
    result = await session.execute(
        text(
            "UPDATE subscriptions SET status = :expired, updated_at = now() "
            "WHERE status IN (:active, :cancelled) AND expires_at <= now() "
            "RETURNING user_id"
        ),
        {"expired": SUB_EXPIRED, "active": SUB_ACTIVE, "cancelled": SUB_CANCELLED},
    )
    users = [row[0] for row in result.fetchall()]
    for user_id in users:
        await track(session, "subscription_expired", user_id=user_id)
    if users:
        log.info("подписки истекли", количество=len(users))
    return users
