"""Нижнее меню — то, что видно всегда.

Обычная reply-клавиатура, а не инлайн: инлайн живёт при своём сообщении и
уезжает вверх вместе с ним, а меню должно быть под рукой на любом шаге
разговора. Кнопки — это текст, поэтому роутер меню включается раньше
разговорного, иначе «Настройки» уедут в круг как реплика.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.bot.texts import ru

# Разделы, которые уже есть. «Прогресс» и «Помощь» добавятся своими этапами:
# кнопка, за которой пусто, хуже отсутствующей.
#
# Две строки, а не одна: в один ряд Telegram ужимает надписи до нечитаемого,
# а «Подписка» отдельной строкой не теряется среди повседневных разделов.
MENU_ROWS = (
    (ru.MENU_TALK, ru.MENU_PROFILE),
    (ru.MENU_SUBSCRIPTION,),
)

MENU_BUTTONS = tuple(title for row in MENU_ROWS for title in row)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=title) for title in row] for row in MENU_ROWS],
        resize_keyboard=True,
        # Поле ввода остаётся главным: человек сюда пришёл говорить, а не жать.
        input_field_placeholder=ru.MENU_PLACEHOLDER,
    )
