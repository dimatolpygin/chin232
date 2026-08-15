"""Дневные лимиты бесплатного доступа.

Считаем действия, а не токены и не минуты: юзер видит и контролирует только их.
Оперативный счётчик живёт в redis (быстро, сам умирает в местную полночь),
история дублируется в `daily_usage`, числа лежат в `settings` и меняются из
админки без перезапуска.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import track
from app.db.models import Setting, User
from app.logging import get_logger

log = get_logger("limits")

# Два счётчика, а не один: разбор произношения — самая дорогая операция
# проекта, и тратить на него дневную норму разговора нельзя.
KIND_MESSAGE = "messages"
KIND_CHECK = "checks"

SETTINGS_KEY = "limits"

# Запасные значения из ТЗ (7.6). Нужны, только если строки настроек нет: без
# них первое же сообщение упало бы вместо того, чтобы работать по умолчанию.
DEFAULTS = {
    "trial_days": 3,
    "trial_messages": 30,
    "trial_checks": 3,
    "messages": 10,
    "checks": 3,
}

DEFAULT_TZ = "Europe/Moscow"


@dataclass(slots=True)
class Limits:
    trial_days: int
    trial_messages: int
    trial_checks: int
    messages: int
    checks: int


@dataclass(slots=True)
class Quota:
    """Состояние счётчика после проверки или списания."""

    kind: str
    used: int
    limit: int
    trial: bool
    # Ставит вызывающая функция: у проверки и у списания разный смысл «влезли».
    allowed: bool

    @property
    def left(self) -> int:
        return max(self.limit - self.used, 0)


async def get_limits(session: AsyncSession) -> Limits:
    """Прочитать числа лимитов из базы.

    Читаем на каждой проверке и без кэша: это одна выборка по первичному ключу
    рядом с вызовами внешних сервисов на секунды, зато правка из админки
    начинает действовать сразу, а не после перезапуска контейнера.
    """
    row = await session.get(Setting, SETTINGS_KEY)
    raw = row.value if row is not None and isinstance(row.value, dict) else {}
    values: dict[str, int] = {}
    for key, fallback in DEFAULTS.items():
        try:
            values[key] = int(raw.get(key, fallback))
        except (TypeError, ValueError):
            log.warning("значение лимита не число, взято значение по умолчанию", лимит=key)
            values[key] = fallback
    return Limits(**values)


def user_zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("часовой пояс пользователя неизвестен", user_id=str(user.id), пояс=user.tz)
        return ZoneInfo(DEFAULT_TZ)


def local_today(user: User, now: datetime | None = None) -> date_type:
    zone = user_zone(user)
    return (now or datetime.now(zone)).astimezone(zone).date()


def seconds_to_midnight(user: User, now: datetime | None = None) -> int:
    """Сколько секунд до местной полуночи: столько и живёт счётчик в redis."""
    zone = user_zone(user)
    moment = (now or datetime.now(zone)).astimezone(zone)
    midnight = datetime.combine(moment.date() + timedelta(days=1), datetime.min.time(), zone)
    # Не меньше минуты: TTL в ноль или отрицательный redis не примет, а на
    # переходе через полночь разница может оказаться нулевой.
    return max(int((midnight - moment).total_seconds()), 60)


def is_trial(user: User, limits: Limits, now: datetime | None = None) -> bool:
    """Пробный период считаем календарными днями, а не часами.

    Иначе юзер, зарегистрировавшийся вечером, теряет треть первого дня и не
    понимает, почему «три дня» кончились через два с половиной.
    """
    if limits.trial_days <= 0 or user.created_at is None:
        return False
    zone = user_zone(user)
    started = user.created_at.astimezone(zone).date()
    return (local_today(user, now) - started).days < limits.trial_days


def limit_for(
    user: User, limits: Limits, kind: str, now: datetime | None = None
) -> tuple[int, bool]:
    """Дневная норма и признак пробного периода."""
    trial = is_trial(user, limits, now)
    if kind == KIND_CHECK:
        return (limits.trial_checks if trial else limits.checks), trial
    return (limits.trial_messages if trial else limits.messages), trial


def usage_key(user: User, kind: str, now: datetime | None = None) -> str:
    # Префикс проекта обязателен: redis общий с чужими проектами.
    return get_settings().redis_key("usage", str(user.id), local_today(user, now).isoformat(), kind)


async def peek(queue, session: AsyncSession, user: User, kind: str) -> Quota:
    """Сколько потрачено, ничего не списывая."""
    limits = await get_limits(session)
    limit, trial = limit_for(user, limits, kind)
    raw = await queue.get(usage_key(user, kind))
    used = int(raw or 0)
    return Quota(kind=kind, used=used, limit=limit, trial=trial, allowed=used < limit)


async def consume(queue, session: AsyncSession, user: User, kind: str) -> Quota:
    """Списать одно действие. `allowed=False` — норма на сегодня исчерпана.

    Списываем ДО работы, а не после: платный вызов, сделанный «в долг», уже
    оплачен, отменить его нечем.
    """
    limits = await get_limits(session)
    limit, trial = limit_for(user, limits, kind)
    key = usage_key(user, kind)

    # INCR атомарен, поэтому два одновременных сообщения не пролезут мимо
    # лимита вдвоём: второй получит своё число и упрётся сам.
    used = int(await queue.incr(key))
    if used == 1:
        await queue.expire(key, seconds_to_midnight(user))

    if used > limit:
        # Возвращаем назад: иначе счётчик убегает вверх на каждой попытке, и
        # после смены лимита из админки юзер остаётся заперт вчерашним перебором.
        await queue.decr(key)
        quota = Quota(kind=kind, used=used - 1, limit=limit, trial=trial, allowed=False)
        await track(
            session,
            "limit_reached",
            user_id=user.id,
            счётчик=kind,
            лимит=limit,
            пробный=trial,
        )
        log.info(
            "лимит исчерпан, платные вызовы не запускаются",
            user_id=str(user.id),
            счётчик=kind,
            израсходовано=quota.used,
            лимит=limit,
            пробный=trial,
        )
        return quota

    await _bump_daily(session, user, kind)
    log.info(
        "списание лимита",
        user_id=str(user.id),
        счётчик=kind,
        израсходовано=used,
        лимит=limit,
        осталось=max(limit - used, 0),
        пробный=trial,
    )
    return Quota(kind=kind, used=used, limit=limit, trial=trial, allowed=True)


# --- напоминание об обновлении лимита ----------------------------------------
#
# Кнопка «Напомнить завтра» обязана действительно напоминать, иначе это обман.
# Заявки лежат в одном хеше redis, а почасовая задача воркера рассылает те, у
# которых местная дата пользователя уже сменилась. Хранить в базе незачем:
# заявка живёт часы и переживать перезапуск redis ей не нужно.


def remind_key() -> str:
    return get_settings().redis_key("remind")


async def ask_remind(queue, user: User, chat_id: int) -> None:
    payload = json.dumps({"chat_id": chat_id, "day": local_today(user).isoformat()})
    await queue.hset(remind_key(), str(user.id), payload)
    log.info("напоминание об обновлении лимита заказано", user_id=str(user.id), chat_id=chat_id)


async def take_due_reminders(queue, session: AsyncSession) -> list[tuple[User, int]]:
    """Забрать заявки, у которых местный день уже сменился.

    Заявка удаляется до отправки: повторно позвать человека хуже, чем не
    позвать вовсе, а отправку он и так увидит следующим сообщением.
    """
    raw = await queue.hgetall(remind_key())
    due: list[tuple[User, int]] = []
    for field, value in (raw or {}).items():
        user_id = field.decode() if isinstance(field, bytes) else field
        value = value.decode() if isinstance(value, bytes) else value
        try:
            data = json.loads(value)
            user = await session.get(User, uuid.UUID(user_id))
        except (ValueError, TypeError):
            await queue.hdel(remind_key(), user_id)
            log.warning("заявка на напоминание не разобралась", user_id=user_id)
            continue
        if user is None:
            await queue.hdel(remind_key(), user_id)
            continue
        if local_today(user).isoformat() > str(data.get("day")):
            await queue.hdel(remind_key(), user_id)
            due.append((user, int(data.get("chat_id") or 0)))
    return due


async def _bump_daily(session: AsyncSession, user: User, kind: str) -> None:
    """Продублировать расход в `daily_usage`.

    Одним запросом с UPSERT, а не «прочитать-посчитать-записать»: две реплики
    подряд идут разными задачами воркера и легко пересекаются.
    """
    await session.execute(
        text(
            "INSERT INTO daily_usage (user_id, date, messages, checks) "
            "VALUES (:user_id, :day, :messages, :checks) "
            "ON CONFLICT (user_id, date) DO UPDATE SET "
            "messages = daily_usage.messages + EXCLUDED.messages, "
            "checks = daily_usage.checks + EXCLUDED.checks"
        ),
        {
            "user_id": user.id,
            "day": local_today(user),
            "messages": 1 if kind == KIND_MESSAGE else 0,
            "checks": 1 if kind == KIND_CHECK else 0,
        },
    )
