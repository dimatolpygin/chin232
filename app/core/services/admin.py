"""Админка: правка чисел, по которым живёт бот, и выборка адресатов рассылки.

Смысл этапа — клиент крутит лимиты и цену сам, глядя на конверсию. Поэтому
правка обязана действовать **сразу**: числа лежат в базе, `get_limits` читает
их без кэша на каждой проверке, и следующее же сообщение юзера считается по
новым. Перезапуск контейнера не нужен и не подразумевается.

Логика здесь, а не в хендлере: тот же набор ручек открывает вебапп админки,
и второй раз описывать, что «минимум ноль, а максимум пятьсот», никто не будет.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import track
from app.core.services.limits import DEFAULTS, SETTINGS_KEY, Limits, get_limits
from app.core.services.stats import WEEK, today_msk
from app.db.models import DailyUsage, Identity, Plan, Setting, Subscription, User
from app.db.models.billing import SUB_ACTIVE, SUB_CANCELLED
from app.db.repositories.users import PROVIDER_TELEGRAM
from app.logging import get_logger

log = get_logger("admin")


# --- кто такой админ -----------------------------------------------------------
#
# Два списка. Первый — переменная окружения: его нельзя изменить из бота, и
# именно он остаётся, если в базе что-то напутали. Второй — выданные командой
# `/admin_add`, лежат в `settings`. Такой порядок специально: полный доступ к
# ценам и рассылке не должен зависеть только от строки в таблице, но и звать
# разработчика ради нового сотрудника клиенту незачем.

ADMINS_KEY = "admins"


def config_admins() -> set[int]:
    """Несменяемые админы из переменной окружения."""
    return set(get_settings().admin_id_list)


async def stored_admins(session: AsyncSession) -> list[int]:
    """Админы, выданные командой. Мусор в строке молча пропускаем."""
    row = await session.get(Setting, ADMINS_KEY)
    значения = row.value if row is not None and isinstance(row.value, list) else []
    ids: list[int] = []
    for значение in значения:
        try:
            ids.append(int(значение))
        except (TypeError, ValueError):
            log.warning("в списке админов не число, запись пропущена", значение=str(значение))
    return ids


async def admin_ids(session: AsyncSession) -> set[int]:
    return config_admins() | set(await stored_admins(session))


async def is_admin(session: AsyncSession, telegram_id: int | None) -> bool:
    """Пускать ли в админку."""
    return bool(telegram_id) and telegram_id in await admin_ids(session)


async def add_admin(session: AsyncSession, telegram_id: int, by: int | None = None) -> bool:
    """Выдать доступ. False — он уже есть."""
    if telegram_id in await admin_ids(session):
        return False
    ids = [*await stored_admins(session), telegram_id]
    row = await session.get(Setting, ADMINS_KEY)
    if row is None:
        session.add(Setting(key=ADMINS_KEY, value=ids))
    else:
        # Новый список, а не append: правка JSONB на месте до базы не доезжает.
        row.value = ids
    await session.flush()
    await track(session, "admin_added", кому=telegram_id, кем=by)
    log.warning("выдан доступ в админку", кому=telegram_id, кем=by)
    return True


async def remove_admin(session: AsyncSession, telegram_id: int, by: int | None = None) -> bool:
    """Отобрать доступ. False — админ прописан в конфиге, из бота его не убрать.

    Это защита от потери контроля: иначе один неверный `/admin_del` мог бы
    оставить бота без единого админа, и вернуть доступ было бы нечем.
    """
    if telegram_id in config_admins():
        log.warning("попытка убрать админа из конфига отклонена", кому=telegram_id, кем=by)
        return False
    ids = [i for i in await stored_admins(session) if i != telegram_id]
    row = await session.get(Setting, ADMINS_KEY)
    if row is not None:
        row.value = ids
        await session.flush()
    await track(session, "admin_removed", кому=telegram_id, кем=by)
    log.warning("доступ в админку отобран", кому=telegram_id, кем=by)
    return True


async def resolve_telegram_id(session: AsyncSession, raw: str) -> int | None:
    """Понять, о ком речь: число или `@username`.

    Имя ищем в `identities`: свою колонку с логинами заводить незачем, а
    человек, ни разу не написавший боту, в админы всё равно не годится —
    писать ему бот не сможет.
    """
    значение = (raw or "").strip()
    if not значение:
        return None
    if значение.lstrip("-").isdigit():
        return int(значение)
    имя = значение.lstrip("@").lower()
    ext_id = await session.scalar(
        select(Identity.ext_id).where(
            Identity.provider == PROVIDER_TELEGRAM,
            func.lower(Identity.username) == имя,
        )
    )
    try:
        return int(ext_id) if ext_id else None
    except (TypeError, ValueError):
        return None


async def admin_names(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    """Логины админов для экрана: голый id ничего не говорит о человеке."""
    if not ids:
        return {}
    rows = await session.execute(
        select(Identity.ext_id, Identity.username, Identity.first_name).where(
            Identity.provider == PROVIDER_TELEGRAM,
            Identity.ext_id.in_([str(i) for i in ids]),
        )
    )
    names: dict[int, str] = {}
    for ext_id, username, first_name in rows:
        try:
            names[int(ext_id)] = f"@{username}" if username else (first_name or "")
        except (TypeError, ValueError):
            continue
    return names


# --- редактируемые числа -------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Knob:
    """Ручка в админке: что за число, в каких пределах и с каким шагом."""

    key: str
    title: str
    unit: str
    step: int
    big: int
    minimum: int
    maximum: int

    def clamp(self, value: int) -> int:
        return max(self.minimum, min(self.maximum, value))


# Порядок — как на экране. Сначала то, что клиент крутит каждый день (дневные
# лимиты), потом пробный период.
#
# Шаг задан парой: мелкий для подгонки, крупный чтобы не жать двадцать раз.
# Минимум ноль везде намеренно — «бесплатного доступа нет» это законная
# настройка, а не ошибка; предупредить о ней должен экран, а не запрет.
KNOBS: tuple[Knob, ...] = (
    Knob("messages", "Сообщений в день", "шт", 1, 5, 0, 500),
    Knob("checks", "Разборов в день", "шт", 1, 5, 0, 100),
    Knob("trial_days", "Пробный период", "дн", 1, 3, 0, 90),
    Knob("trial_messages", "Сообщений в пробном", "шт", 5, 20, 0, 1000),
    Knob("trial_checks", "Разборов в пробном", "шт", 1, 5, 0, 100),
)

KNOBS_BY_KEY = {knob.key: knob for knob in KNOBS}

# Цена — не лимит: она живёт в тарифе, а не в настройках, и у каждого тарифа
# своя. Шаги в рублях.
PRICE_STEP = 10
PRICE_BIG = 100
PRICE_MAX = Decimal("100000")


def current_value(limits: Limits, key: str) -> int:
    return int(getattr(limits, key, DEFAULTS.get(key, 0)))


async def change_limit(session: AsyncSession, key: str, delta: int) -> Limits:
    """Подвинуть число лимита на `delta`. Возвращает лимиты целиком, уже новые.

    Пишем весь словарь заново, а не одно поле: JSONB в SQLAlchemy меняется
    только присвоением нового объекта, правка на месте до базы не доезжает —
    и админ видел бы «сохранено», а бот считал по-старому.
    """
    knob = KNOBS_BY_KEY.get(key)
    if knob is None:
        log.warning("правка неизвестного лимита отклонена", лимит=key)
        return await get_limits(session)

    limits = await get_limits(session)
    было = current_value(limits, key)
    стало = knob.clamp(было + delta)
    if стало == было:
        return limits

    значения = {k: current_value(limits, k) for k in DEFAULTS}
    значения[key] = стало
    row = await session.get(Setting, SETTINGS_KEY)
    if row is None:
        session.add(Setting(key=SETTINGS_KEY, value=значения))
    else:
        row.value = значения
    await session.flush()

    await track(session, "admin_limit_changed", лимит=key, было=было, стало=стало)
    log.info("лимит изменён из админки", лимит=key, название=knob.title, было=было, стало=стало)
    return Limits(**значения)


async def change_price(session: AsyncSession, code: str, delta: int) -> Plan | None:
    """Подвинуть цену тарифа. Ниже нуля не пускаем.

    Здесь меняется только та цена, которую видит юзер в боте. Списывает
    платёжка свою — ту, что задана в оффере lava.top. Разъехавшиеся цены
    ловятся при выставлении счёта и пишутся в лог предупреждением, но
    предупредить админа обязан сам экран.
    """
    plan = await session.get(Plan, code)
    if plan is None:
        log.warning("правка цены несуществующего тарифа отклонена", тариф=code)
        return None

    было = Decimal(plan.price)
    стало = (было + Decimal(delta)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    стало = max(Decimal("0"), min(PRICE_MAX, стало))
    if стало == было:
        return plan

    plan.price = стало
    await session.flush()
    await track(session, "admin_price_changed", тариф=code, было=float(было), стало=float(стало))
    log.info(
        "цена тарифа изменена из админки",
        тариф=code,
        было=float(было),
        стало=float(стало),
        валюта=plan.currency,
    )
    return plan


async def editable_plans(session: AsyncSession) -> list[Plan]:
    """Тарифы, которым есть смысл править цену: включённые."""
    result = await session.execute(
        select(Plan).where(Plan.active.is_(True)).order_by(Plan.autorenew.desc(), Plan.price)
    )
    return list(result.scalars().all())


# --- адресаты рассылки ---------------------------------------------------------

SEGMENT_ALL = "all"
SEGMENT_PAYING = "paying"
SEGMENT_FREE = "free"
SEGMENT_ACTIVE = "active"

SEGMENT_TITLES: dict[str, str] = {
    SEGMENT_ALL: "Всем",
    SEGMENT_PAYING: "Платящим",
    SEGMENT_FREE: "Без подписки",
    SEGMENT_ACTIVE: "Занимались за неделю",
}


def _paying_ids() -> Select:
    return select(Subscription.user_id).where(
        Subscription.status.in_([SUB_ACTIVE, SUB_CANCELLED]),
        Subscription.expires_at > func.now(),
    )


def _audience_stmt(segment: str) -> Select:
    """Кому пишем. Возвращает пары «кто» и «куда».

    Адрес берётся из `identities`, а не из `users`: telegram_id там и лежит,
    и человек без телеграм-входа в рассылку просто не попадёт, а не сломает её.
    """
    stmt = select(User.id, Identity.ext_id).join(
        Identity,
        and_(Identity.user_id == User.id, Identity.provider == PROVIDER_TELEGRAM),
    )
    if segment == SEGMENT_PAYING:
        return stmt.where(User.id.in_(_paying_ids()))
    if segment == SEGMENT_FREE:
        return stmt.where(User.id.not_in(_paying_ids()))
    if segment == SEGMENT_ACTIVE:
        неделя = today_msk() - timedelta(days=WEEK - 1)
        return stmt.where(User.id.in_(select(DailyUsage.user_id).where(DailyUsage.date >= неделя)))
    return stmt


async def audience(session: AsyncSession, segment: str) -> list[tuple[str, int]]:
    """Список адресатов сегмента: (user_id строкой, chat_id числом)."""
    rows = await session.execute(_audience_stmt(segment))
    люди: list[tuple[str, int]] = []
    for user_id, ext_id in rows:
        try:
            люди.append((str(user_id), int(ext_id)))
        except (TypeError, ValueError):
            # Чужой формат идентификатора — не повод ронять всю рассылку.
            log.warning("адресат пропущен: telegram_id не число", user_id=str(user_id))
    return люди


async def audience_size(session: AsyncSession, segment: str) -> int:
    """Сколько человек в сегменте. Отдельным счётом: список может быть большим."""
    stmt = _audience_stmt(segment).with_only_columns(func.count()).order_by(None)
    return int(await session.scalar(stmt) or 0)
