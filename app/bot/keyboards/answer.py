"""Кнопки под голосовым ответом бота: «Текст», «Помощь», «Оценка»."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import ru

ACTION_PREFIX = "ans"
ACTION_TEXT = "txt"
ACTION_HELP = "help"
# Включить режим «повторите за мной» по этой реплике.
ACTION_PRON = "pron"
# Переслать голос эталона ещё раз.
ACTION_LISTEN = "say"
# Записать попытку заново после показанного результата.
ACTION_AGAIN = "again"
# Выйти из режима, не записывая ничего.
ACTION_CANCEL = "stop"


def parse_action(data: str) -> tuple[str, int] | None:
    """Разобрать callback_data. None, если формат чужой или id не число."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != ACTION_PREFIX:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def _button(text: str, action: str, dialog_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{ACTION_PREFIX}:{action}:{dialog_id}")


def answer_keyboard(
    dialog_id: int,
    *,
    with_text: bool = True,
    with_help: bool = True,
    with_pron: bool = True,
) -> InlineKeyboardMarkup | None:
    """Клавиатура ответа. Нажатая кнопка убирается — так повтор невозможен.

    Это и есть защита от дублей: показанный разбор остаётся на экране, а второй
    раз нажать уже нечего. Клавиатура целиком пропадает, когда нажаты все.
    «Оценка» вынесена во второй ряд: три кнопки в строке на телефоне режутся
    по ширине до нечитаемых огрызков.
    """
    rows: list[list[InlineKeyboardButton]] = []
    first: list[InlineKeyboardButton] = []
    if with_text:
        first.append(_button(ru.BTN_TEXT, ACTION_TEXT, dialog_id))
    if with_help:
        first.append(_button(ru.BTN_HELP, ACTION_HELP, dialog_id))
    if first:
        rows.append(first)
    if with_pron:
        rows.append([_button(ru.BTN_PRON, ACTION_PRON, dialog_id)])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def practice_keyboard(dialog_id: int) -> InlineKeyboardMarkup:
    """Под эталоном: послушать ещё раз или выйти из режима."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(ru.BTN_LISTEN, ACTION_LISTEN, dialog_id)],
            [_button(ru.BTN_CANCEL, ACTION_CANCEL, dialog_id)],
        ]
    )


def result_keyboard(dialog_id: int) -> InlineKeyboardMarkup:
    """Под результатом: переслушать эталон или сразу записать ещё попытку."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(ru.BTN_LISTEN, ACTION_LISTEN, dialog_id),
                _button(ru.BTN_AGAIN, ACTION_AGAIN, dialog_id),
            ]
        ]
    )
