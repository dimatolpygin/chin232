"""Клавиатура выбора уровня HSK."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

LEVEL_PREFIX = "hsk"

LEVELS = [
    ("hsk12", "HSK 1-2 — начинающий"),
    ("hsk34", "HSK 3-4 — средний"),
    ("hsk56", "HSK 5-6 — продвинутый"),
    ("unknown", "Не знаю свой уровень"),
]


def hsk_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=f"{LEVEL_PREFIX}:{code}")]
            for code, title in LEVELS
        ]
    )
