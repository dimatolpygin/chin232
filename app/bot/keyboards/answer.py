"""Кнопки под голосовым ответом бота: «Текст» и «Помощь»."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import ru

ACTION_PREFIX = "ans"
ACTION_TEXT = "txt"
ACTION_HELP = "help"


def parse_action(data: str) -> tuple[str, int] | None:
    """Разобрать callback_data. None, если формат чужой или id не число."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != ACTION_PREFIX:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def answer_keyboard(
    dialog_id: int, *, with_text: bool = True, with_help: bool = True
) -> InlineKeyboardMarkup | None:
    """Клавиатура ответа. Нажатая кнопка убирается — так повтор невозможен.

    Это и есть защита от дублей: показанный разбор остаётся на экране, а второй
    раз нажать уже нечего. Клавиатура целиком пропадает, когда обе кнопки нажаты.
    """
    row: list[InlineKeyboardButton] = []
    if with_text:
        row.append(
            InlineKeyboardButton(
                text=ru.BTN_TEXT, callback_data=f"{ACTION_PREFIX}:{ACTION_TEXT}:{dialog_id}"
            )
        )
    if with_help:
        row.append(
            InlineKeyboardButton(
                text=ru.BTN_HELP, callback_data=f"{ACTION_PREFIX}:{ACTION_HELP}:{dialog_id}"
            )
        )
    if not row:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[row])
