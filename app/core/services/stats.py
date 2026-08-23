"""Статистика для админки: юзеры, деньги, расход на внешние сервисы.

Считается на лету запросами к базе, без витрин и кэшей: сотня-другая юзеров
считается за миллисекунды, а устаревшее число в админке хуже медленного —
по нему клиент крутит лимиты.

Логика здесь, а не в хендлере: те же цифры пойдут в вебапп и в утренний отчёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.services.limits import DEFAULT_TZ
from app.db.models import DailyUsage, Payment, Subscription, User
from app.db.models.billing import PAY_COMPLETED, SUB_ACTIVE, SUB_CANCELLED
from app.logging import get_logger

log = get_logger("stats")

WEEK = 7
# Окно выручки: месяц — тот срок, за который клиент считает свои деньги.
REVENUE_DAYS = 30


@dataclass(slots=True, frozen=True)
class Summary:
    """Сводка по продукту на текущий момент."""

    users_total: int
    users_today: int
    users_week: int
    active_today: int
    active_week: int
    paying: int
    messages_today: int
    checks_today: int
    revenue: dict[str, Decimal] = field(default_factory=dict)

    @property
    def conversion(self) -> float:
        """Доля платящих от всех заведённых, проценты."""
        if not self.users_total:
            return 0.0
        return self.paying / self.users_total * 100


@dataclass(slots=True, frozen=True)
class Spend:
    """Расход по одному внешнему сервису за период."""

    provider: str
    calls: int
    errors: int
    units: float
    unit: str
    cost: float


def today_msk() -> date_type:
    """Сегодня по Москве.

    Дни в `daily_usage` — местные даты пользователей, и складывать их можно
    только по одному календарю. Московский выбран потому, что по нему живёт
    заказчик и почти все юзеры; для сводки этой точности достаточно.
    """
    return datetime.now(ZoneInfo(DEFAULT_TZ)).date()


def _midnight(day: date_type) -> datetime:
    """Московская полночь этого дня.

    Сравниваем с ней, а не с `date(created_at)`: у той даты часовой пояс —
    какой стоит у постгреса, и «новых сегодня» считалось бы по чужому
    календарю, разъезжаясь с активностью из `daily_usage`.
    """
    return datetime.combine(day, time.min, ZoneInfo(DEFAULT_TZ))


async def load_summary(session: AsyncSession) -> Summary:
    today = today_msk()
    week_ago = today - timedelta(days=WEEK - 1)
    начало_дня = _midnight(today)
    неделю_назад = _midnight(week_ago)

    users_total = await session.scalar(select(func.count()).select_from(User)) or 0
    # Новых считаем по created_at, а не по первому сообщению: человек, который
    # нажал /start и ушёл, — это тоже результат рекламы, и он должен быть виден.
    users_today = (
        await session.scalar(
            select(func.count()).select_from(User).where(User.created_at >= начало_дня)
        )
        or 0
    )
    users_week = (
        await session.scalar(
            select(func.count()).select_from(User).where(User.created_at >= неделю_назад)
        )
        or 0
    )

    active_today = (
        await session.scalar(
            select(func.count(func.distinct(DailyUsage.user_id))).where(DailyUsage.date == today)
        )
        or 0
    )
    active_week = (
        await session.scalar(
            select(func.count(func.distinct(DailyUsage.user_id))).where(DailyUsage.date >= week_ago)
        )
        or 0
    )

    # Платящие — те, у кого доступ действует прямо сейчас. Смотрим на дату, а
    # не на статус: статус переставляет почасовая задача, и между запусками
    # просроченная подписка ещё числится активной.
    paying = (
        await session.scalar(
            select(func.count(func.distinct(Subscription.user_id))).where(
                Subscription.status.in_([SUB_ACTIVE, SUB_CANCELLED]),
                Subscription.expires_at > func.now(),
            )
        )
        or 0
    )

    сегодня = (
        await session.execute(
            select(
                func.coalesce(func.sum(DailyUsage.messages), 0),
                func.coalesce(func.sum(DailyUsage.checks), 0),
            ).where(DailyUsage.date == today)
        )
    ).one()

    # По валютам отдельно: платёжка умеет и рубли, и доллары, и складывать их
    # в одно число нельзя.
    выручка = await session.execute(
        select(Payment.currency, func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.status == PAY_COMPLETED,
            Payment.paid_at >= datetime.now(ZoneInfo(DEFAULT_TZ)) - timedelta(days=REVENUE_DAYS),
        )
        .group_by(Payment.currency)
    )

    summary = Summary(
        users_total=users_total,
        users_today=users_today,
        users_week=users_week,
        active_today=active_today,
        active_week=active_week,
        paying=paying,
        messages_today=int(сегодня[0] or 0),
        checks_today=int(сегодня[1] or 0),
        revenue={(cur or "RUB"): Decimal(amount or 0) for cur, amount in выручка},
    )
    log.info(
        "статистика собрана",
        юзеров=summary.users_total,
        активных_сегодня=summary.active_today,
        платящих=summary.paying,
        конверсия=round(summary.conversion, 1),
    )
    return summary


async def load_spending(session: AsyncSession, days: int) -> list[Spend]:
    """Расход по каждому сервису за последние `days` суток, дорогие сверху."""
    rows = await session.execute(
        text(
            "SELECT provider, count(*) AS calls, "
            "count(*) FILTER (WHERE NOT ok) AS errors, "
            "coalesce(sum(units), 0) AS units, "
            "max(unit) AS unit, "
            "coalesce(sum(cost), 0) AS cost "
            "FROM service_calls "
            "WHERE created_at >= :since "
            "GROUP BY provider ORDER BY cost DESC"
        ),
        # Границу периода считаем в питоне: интервал, собранный в SQL из
        # параметра, драйверу нечем типизировать, и запрос падает на пустом месте.
        {"since": datetime.now(ZoneInfo(DEFAULT_TZ)) - timedelta(days=days)},
    )
    spending = [
        Spend(
            provider=row.provider,
            calls=int(row.calls),
            errors=int(row.errors or 0),
            units=float(row.units or 0),
            unit=row.unit or "",
            cost=float(row.cost or 0),
        )
        for row in rows
    ]
    log.info(
        "расход по сервисам собран",
        дней=days,
        сервисов=len(spending),
        всего_долларов=round(sum(s.cost for s in spending), 4),
    )
    return spending
