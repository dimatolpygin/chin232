"""Партнёры: кто привёл юзера и сколько он за это заработал.

Правила, на которых всё стоит (договорённость с клиентом):

- юзер закрепляется за партнёром **один раз**, при первом входе по ссылке
  `?start=ref_<код>`. Уже знакомого боту юзера чужая ссылка не переманивает;
- процент считается **со всех оплат** закреплённого юзера, включая
  автопродления, бессрочно;
- бот только считает, сколько причитается. Выплаты делает клиент сам.

Какой партнёр у платежа, решает снимок `payments.partner_id`, а не текущая
привязка юзера: отчёт за прошлый месяц не должен меняться задним числом.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import track
from app.core.services.stats import _midnight, today_msk
from app.db.models import Partner, Payment, User
from app.db.models.billing import PAY_COMPLETED
from app.logging import get_logger

log = get_logger("partners")

REF_PREFIX = "ref_"
# Параметр старта Telegram пропускает только латиницу, цифры, `_` и `-`.
CODE_RE = re.compile(r"^[a-z0-9_-]{2,32}$")
MAX_NAME = 128

PERIOD_ALL = "all"
PERIOD_MONTH = "month"
PERIOD_PREV = "prev"
PERIODS = (PERIOD_ALL, PERIOD_MONTH, PERIOD_PREV)


class PartnerError(ValueError):
    """Команду админа выполнить нельзя. Текст пригоден для показа."""


@dataclass(slots=True, frozen=True)
class Report:
    """Что партнёр принёс за период."""

    partner: Partner
    period: str
    users: int
    paid_users: int
    payments: int
    revenue: dict[str, Decimal] = field(default_factory=dict)

    @property
    def due(self) -> dict[str, Decimal]:
        """Сколько причитается партнёру, по каждой валюте отдельно."""
        доля = Decimal(self.partner.percent) / Decimal(100)
        return {
            cur: (amount * доля).quantize(Decimal("0.01")) for cur, amount in self.revenue.items()
        }


def normalize_code(raw: str | None) -> str | None:
    """Код из ссылки или команды в каноническом виде, либо None, если это мусор."""
    code = (raw or "").strip().lower()
    return code if CODE_RE.match(code) else None


def code_from_start(args: str | None) -> str | None:
    """Код партнёра из параметра `/start`. Всё, что не `ref_<код>`, — не наше."""
    value = (args or "").strip()
    if not value.lower().startswith(REF_PREFIX):
        return None
    return normalize_code(value[len(REF_PREFIX) :])


def partner_link(code: str) -> str:
    return f"{get_settings().bot_link}?start={REF_PREFIX}{code}"


async def find_partner(session: AsyncSession, code: str) -> Partner | None:
    return await session.scalar(select(Partner).where(Partner.code == code).limit(1))


async def attach_by_start(
    session: AsyncSession, user: User, is_new_user: bool, args: str | None
) -> Partner | None:
    """Закрепить юзера за партнёром по параметру `/start`. None — не закрепили.

    Закрепляется только новый юзер. Старого, даже без партнёра, по ссылке не
    закрепляем: клиент привёл его сам, и платить за него блогеру не за что.
    """
    code = code_from_start(args)
    if code is None:
        return None

    if not is_new_user or user.partner_id is not None:
        log.info(
            "ссылка партнёра от уже знакомого юзера, закрепление не меняется",
            user_id=str(user.id),
            код=code,
            партнёр_уже=str(user.partner_id) if user.partner_id else None,
        )
        await track(session, "partner_ref_ignored", user_id=user.id, код=code)
        return None

    partner = await find_partner(session, code)
    if partner is None or not partner.active:
        log.warning(
            "ссылка с неизвестным или отключённым партнёром",
            user_id=str(user.id),
            код=code,
            найден=partner is not None,
        )
        await track(session, "partner_ref_unknown", user_id=user.id, код=code)
        return None

    user.partner_id = partner.id
    await track(session, "partner_attached", user_id=user.id, партнёр=partner.code)
    log.info(
        "юзер закреплён за партнёром",
        user_id=str(user.id),
        партнёр=partner.code,
        имя_партнёра=partner.name,
    )
    return partner


async def save_partner(
    session: AsyncSession, code_raw: str, percent_raw: str, name: str
) -> tuple[Partner, bool]:
    """Завести партнёра или поправить процент и имя у существующего.

    Возвращает партнёра и признак «заведён впервые». Правка процента меняет и
    расчёт по прошлым оплатам: процент — условие договора, а не снимок.
    """
    code = normalize_code(code_raw)
    if code is None:
        raise PartnerError("code")
    try:
        percent = Decimal(percent_raw.replace(",", ".").rstrip("%"))
    except (ArithmeticError, ValueError) as exc:
        raise PartnerError("percent") from exc
    if not Decimal(0) <= percent <= Decimal(100):
        raise PartnerError("percent")
    name = name.strip()[:MAX_NAME]
    if not name:
        raise PartnerError("name")

    partner = await find_partner(session, code)
    created = partner is None
    if partner is None:
        partner = Partner(code=code, name=name, percent=percent, active=True)
        session.add(partner)
    else:
        partner.name = name
        partner.percent = percent
        partner.active = True
    await session.flush()
    return partner, created


async def set_active(session: AsyncSession, code_raw: str, active: bool) -> Partner | None:
    code = normalize_code(code_raw)
    partner = await find_partner(session, code) if code else None
    if partner is not None:
        partner.active = active
        await session.flush()
    return partner


async def list_partners(session: AsyncSession) -> list[Partner]:
    result = await session.execute(select(Partner).order_by(Partner.created_at))
    return list(result.scalars().all())


def period_bounds(
    period: str, today: date_type | None = None
) -> tuple[datetime | None, datetime | None]:
    """Границы периода по московскому календарю: [начало, конец)."""
    if period == PERIOD_ALL:
        return None, None
    today = today or today_msk()
    first = today.replace(day=1)
    if period == PERIOD_PREV:
        prev_first = (first - timedelta(days=1)).replace(day=1)
        return _midnight(prev_first), _midnight(first)
    next_first = (first + timedelta(days=32)).replace(day=1)
    return _midnight(first), _midnight(next_first)


async def build_report(session: AsyncSession, partner: Partner, period: str) -> Report:
    """Отчёт по партнёру за период.

    «Пришли» — юзеры, заведённые за период (закрепление случается при
    заведении). Деньги — оплаченные платежи со снимком этого партнёра;
    возвраты сюда не попадают, у них статус уже не `completed`.
    """
    since, until = period_bounds(period)

    users_q = select(func.count()).select_from(User).where(User.partner_id == partner.id)
    paid_q = select(Payment.currency, func.count(), func.coalesce(func.sum(Payment.amount), 0))
    payers_q = select(func.count(func.distinct(Payment.user_id)))
    платёж = (Payment.partner_id == partner.id, Payment.status == PAY_COMPLETED)
    paid_q = paid_q.where(*платёж)
    payers_q = payers_q.where(*платёж)
    if since is not None:
        users_q = users_q.where(User.created_at >= since, User.created_at < until)
        paid_q = paid_q.where(Payment.paid_at >= since, Payment.paid_at < until)
        payers_q = payers_q.where(Payment.paid_at >= since, Payment.paid_at < until)

    users = await session.scalar(users_q) or 0
    payers = await session.scalar(payers_q) or 0
    revenue: dict[str, Decimal] = {}
    payments = 0
    for currency, count, amount in (await session.execute(paid_q.group_by(Payment.currency))).all():
        payments += count
        revenue[currency or "RUB"] = Decimal(amount)

    return Report(
        partner=partner,
        period=period,
        users=users,
        paid_users=payers,
        payments=payments,
        revenue=revenue,
    )
