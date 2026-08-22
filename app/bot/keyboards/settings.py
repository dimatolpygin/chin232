"""Экраны раздела «Настройки».

Каждая настройка — свой экран, а не одна простыня на двадцать кнопок: голосов
шесть, тем семь, и вместе они не помещаются на экран телефона.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import ru
from app.core.providers.base import Voice
from app.core.services.settings import SPEEDS, TOPICS

SET_PREFIX = "set"

SECTION_MENU = "menu"
SECTION_LEVEL = "level"
SECTION_VOICE = "voice"
SECTION_SPEED = "speed"
SECTION_TOPIC = "topic"

ACTION_OPEN = "open"
ACTION_PICK = "pick"
ACTION_PLAY = "play"

# Уровни без варианта «не знаю»: он нужен только на первом экране, когда
# человек ещё не пробовал. В настройках выбор уже осознанный.
LEVELS = ("hsk12", "hsk34", "hsk56")


@dataclass(slots=True, frozen=True)
class SettingsAction:
    section: str
    action: str
    value: str


def parse_settings_action(data: str) -> SettingsAction | None:
    """Разобрать `set:<раздел>[:<значение>]` и `set:voice:play:<id>`."""
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != SET_PREFIX:
        return None
    if len(parts) == 2:
        return SettingsAction(parts[1], ACTION_OPEN, "")
    if len(parts) == 3:
        return SettingsAction(parts[1], ACTION_PICK, parts[2])
    return SettingsAction(parts[1], parts[2], ":".join(parts[3:]))


def _mark(title: str, chosen: bool) -> str:
    """Галочка у выбранного. Без неё экран не отвечает на вопрос «а что сейчас»."""
    return f"✅ {title}" if chosen else title


def _back() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=ru.BTN_BACK, callback_data=f"{SET_PREFIX}:{SECTION_MENU}")]


def settings_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"{SET_PREFIX}:{section}")]
        for section, title in (
            (SECTION_LEVEL, ru.BTN_SET_LEVEL),
            (SECTION_VOICE, ru.BTN_SET_VOICE),
            (SECTION_SPEED, ru.BTN_SET_SPEED),
            (SECTION_TOPIC, ru.BTN_SET_TOPIC),
        )
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def level_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_mark(ru.LEVEL_TITLES[code], code == current),
                callback_data=f"{SET_PREFIX}:{SECTION_LEVEL}:{code}",
            )
        ]
        for code in LEVELS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back()])


def voice_keyboard(voices: tuple[Voice, ...], current_id: str | None) -> InlineKeyboardMarkup:
    """Голос и кнопка прослушивания в одной строке.

    Выбрать вслепую нельзя: названия вроде «мужской спокойный» ничего не говорят
    про то, как это звучит по-китайски.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=_mark(voice.title, voice.id == current_id),
                callback_data=f"{SET_PREFIX}:{SECTION_VOICE}:{voice.id}",
            ),
            InlineKeyboardButton(
                text=ru.BTN_VOICE_PLAY,
                callback_data=f"{SET_PREFIX}:{SECTION_VOICE}:{ACTION_PLAY}:{voice.id}",
            ),
        ]
        for voice in voices
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back()])


def speed_keyboard(current_code: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=_mark(speed.title, speed.code == current_code),
            callback_data=f"{SET_PREFIX}:{SECTION_SPEED}:{speed.code}",
        )
        for speed in SPEEDS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row, _back()])


def topic_keyboard(current_code: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=_mark(topic.title, topic.code == current_code),
            callback_data=f"{SET_PREFIX}:{SECTION_TOPIC}:{topic.code}",
        )
        for topic in TOPICS
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows + [_back()])
