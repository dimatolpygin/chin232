"""Экраны админки.

Каждое число правится на своём экране, как и в настройках юзера: ряд из пяти
кнопок «−100 −10 значение +10 +100» читается с телефона, а простыня из пяти
таких рядов подряд — уже нет.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import ru
from app.core.services.admin import (
    KNOBS,
    PRICE_BIG,
    PRICE_STEP,
    SEGMENT_TITLES,
    Knob,
    current_value,
)
from app.core.services.limits import Limits

ADM_PREFIX = "adm"

SECTION_MENU = "menu"
SECTION_STATS = "stats"
SECTION_LIMITS = "limits"
SECTION_KNOB = "lim"
SECTION_PRICE = "price"
SECTION_PLAN = "pr"
SECTION_SPEND = "spend"
SECTION_LOAD = "load"
SECTION_BROADCAST = "cast"
SECTION_ADMINS = "who"
SECTION_SEND = "send"
SECTION_CANCEL = "cancel"
# Кнопка-табличка со значением: нажимать её незачем, но и молчать она не должна.
SECTION_NOOP = "noop"

# Периоды расхода. Сутки — «что происходит прямо сейчас», месяц — «во что это
# обходится».
SPEND_DAYS = (1, 7, 30)

# Периоды нагрузки в часах: час — «что происходит прямо сейчас», сутки —
# рабочая величина, неделя — чтобы увидеть рост, а не всплеск.
LOAD_HOURS = (1, 24, 168)


@dataclass(slots=True, frozen=True)
class AdminAction:
    section: str
    value: str
    delta: int = 0


def parse_admin_action(data: str) -> AdminAction | None:
    """Разобрать `adm:<раздел>[:<значение>[:<шаг>]]`.

    Шаг приходит со знаком (`adm:lim:messages:-5`), поэтому разбирается числом,
    а не по отдельным кнопкам «плюс» и «минус».
    """
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != ADM_PREFIX:
        return None
    section = parts[1]
    value = parts[2] if len(parts) > 2 else ""
    delta = 0
    if len(parts) > 3:
        try:
            delta = int(parts[3])
        except ValueError:
            return None
    return AdminAction(section=section, value=value, delta=delta)


def _cb(section: str, *rest: object) -> str:
    return ":".join([ADM_PREFIX, section, *(str(part) for part in rest)])


def _back(section: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=ru.BTN_BACK, callback_data=_cb(section))]


def admin_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=ru.BTN_ADMIN_STATS, callback_data=_cb(SECTION_STATS)),
            InlineKeyboardButton(text=ru.BTN_ADMIN_SPEND, callback_data=_cb(SECTION_SPEND, 7)),
        ],
        [
            InlineKeyboardButton(text=ru.BTN_ADMIN_LIMITS, callback_data=_cb(SECTION_LIMITS)),
            InlineKeyboardButton(text=ru.BTN_ADMIN_PRICE, callback_data=_cb(SECTION_PRICE)),
        ],
        [
            InlineKeyboardButton(text=ru.BTN_ADMIN_BROADCAST, callback_data=_cb(SECTION_BROADCAST)),
            InlineKeyboardButton(text=ru.BTN_ADMIN_ADMINS, callback_data=_cb(SECTION_ADMINS)),
        ],
        [InlineKeyboardButton(text=ru.BTN_ADMIN_LOAD, callback_data=_cb(SECTION_LOAD, 24))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admins_keyboard() -> InlineKeyboardMarkup:
    """Экран доступа: список рисуется текстом, руками правится командами."""
    return InlineKeyboardMarkup(inline_keyboard=[_back(SECTION_MENU)])


def limits_keyboard(limits: Limits) -> InlineKeyboardMarkup:
    """Список лимитов с текущими значениями прямо на кнопках."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{knob.title}: {current_value(limits, knob.key)} {knob.unit}",
                callback_data=_cb(SECTION_KNOB, knob.key),
            )
        ]
        for knob in KNOBS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back(SECTION_MENU)])


def knob_keyboard(knob: Knob, value: int) -> InlineKeyboardMarkup:
    """Ряд «−крупный −мелкий значение +мелкий +крупный» для одного числа."""
    row = [
        InlineKeyboardButton(
            text=f"−{knob.big}", callback_data=_cb(SECTION_KNOB, knob.key, -knob.big)
        ),
        InlineKeyboardButton(
            text=f"−{knob.step}", callback_data=_cb(SECTION_KNOB, knob.key, -knob.step)
        ),
        InlineKeyboardButton(text=f"{value} {knob.unit}", callback_data=_cb(SECTION_NOOP)),
        InlineKeyboardButton(
            text=f"+{knob.step}", callback_data=_cb(SECTION_KNOB, knob.key, knob.step)
        ),
        InlineKeyboardButton(
            text=f"+{knob.big}", callback_data=_cb(SECTION_KNOB, knob.key, knob.big)
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row, _back(SECTION_LIMITS)])


def price_keyboard(plans: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{plan.title}: {float(plan.price):.0f} {plan.currency}",
                callback_data=_cb(SECTION_PLAN, plan.code),
            )
        ]
        for plan in plans
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back(SECTION_MENU)])


def plan_keyboard(plan) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=f"−{PRICE_BIG}", callback_data=_cb(SECTION_PLAN, plan.code, -PRICE_BIG)
        ),
        InlineKeyboardButton(
            text=f"−{PRICE_STEP}", callback_data=_cb(SECTION_PLAN, plan.code, -PRICE_STEP)
        ),
        InlineKeyboardButton(
            text=f"{float(plan.price):.0f} {plan.currency}", callback_data=_cb(SECTION_NOOP)
        ),
        InlineKeyboardButton(
            text=f"+{PRICE_STEP}", callback_data=_cb(SECTION_PLAN, plan.code, PRICE_STEP)
        ),
        InlineKeyboardButton(
            text=f"+{PRICE_BIG}", callback_data=_cb(SECTION_PLAN, plan.code, PRICE_BIG)
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row, _back(SECTION_PRICE)])


def spend_keyboard(days: int) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=("✅ " if d == days else "") + ru.SPEND_PERIODS[d],
            callback_data=_cb(SECTION_SPEND, d),
        )
        for d in SPEND_DAYS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row, _back(SECTION_MENU)])


def load_keyboard(hours: int) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=("✅ " if h == hours else "") + ru.LOAD_PERIODS[h],
            callback_data=_cb(SECTION_LOAD, h),
        )
        for h in LOAD_HOURS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row, _back(SECTION_MENU)])


def segments_keyboard(sizes: dict[str, int]) -> InlineKeyboardMarkup:
    """Сегменты рассылки с числом адресатов на кнопке.

    Число обязано быть видно до выбора: «всем» и «платящим» — это очень разные
    решения, и узнавать разницу постфактум поздно.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=f"{title} · {sizes.get(code, 0)}",
                callback_data=_cb(SECTION_BROADCAST, code),
            )
        ]
        for code, title in SEGMENT_TITLES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back(SECTION_MENU)])


def confirm_keyboard(segment: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text=ru.BTN_ADMIN_SEND, callback_data=_cb(SECTION_SEND, segment)),
        InlineKeyboardButton(text=ru.BTN_ADMIN_CANCEL, callback_data=_cb(SECTION_CANCEL)),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])
