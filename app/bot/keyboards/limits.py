"""Кнопки экрана исчерпанного лимита."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import ru

LIMIT_PREFIX = "lim"
# Перейти к оплате. Сама оплата подключается этапом 5.
LIMIT_SUBSCRIBE = "sub"
# Позвать юзера, когда счётчик обнулится.
LIMIT_REMIND = "remind"


def parse_limit_action(data: str) -> str | None:
    parts = (data or "").split(":")
    if len(parts) != 2 or parts[0] != LIMIT_PREFIX:
        return None
    return parts[1]


def limit_keyboard() -> InlineKeyboardMarkup:
    """Стена лимита: подписка и напоминание.

    Каждая кнопка своим рядом: обе длинные, в одну строку на телефоне они
    режутся до нечитаемых огрызков.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ru.BTN_SUBSCRIBE, callback_data=f"{LIMIT_PREFIX}:{LIMIT_SUBSCRIBE}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=ru.BTN_REMIND, callback_data=f"{LIMIT_PREFIX}:{LIMIT_REMIND}"
                )
            ],
        ]
    )
