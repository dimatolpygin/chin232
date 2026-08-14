"""Кнопки «Текст» и «Помощь» под голосовым ответом бота."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.answer import (
    ACTION_HELP,
    ACTION_PREFIX,
    ACTION_TEXT,
    answer_keyboard,
    parse_action,
)
from app.bot.render import esc
from app.bot.texts import ru
from app.config import get_settings
from app.core.services.breakdown import (
    ReplyNotFound,
    Suggestion,
    get_suggestions,
    get_text_breakdown,
)
from app.db.models import User
from app.logging import get_logger

router = Router(name="answer")
log = get_logger("bot")

# Замок живёт минуту: этого хватает на самый медленный платный вызов и не
# успевает помешать осмысленному повтору.
LOCK_TTL_SEC = 60


def _render_help(items: list[Suggestion]) -> str:
    if not items:
        return ru.HELP_EMPTY
    blocks = [
        ru.HELP_ITEM.format(n=i, zh=esc(s.zh), pinyin=esc(s.pinyin), ru=esc(s.ru) or "—")
        for i, s in enumerate(items, 1)
    ]
    return "\n\n".join([ru.HELP_HEADER, *blocks, ru.HELP_FOOTER])


async def _drop_button(callback: CallbackQuery, dialog_id: int, action: str) -> None:
    """Убрать нажатую кнопку, оставив вторую."""
    markup = answer_keyboard(
        dialog_id,
        with_text=action != ACTION_TEXT and _has(callback, ACTION_TEXT),
        with_help=action != ACTION_HELP and _has(callback, ACTION_HELP),
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as exc:
        # Клавиатура уже такая же — значит кнопку успели нажать дважды.
        log.debug("клавиатура не изменилась", причина=str(exc))


def _has(callback: CallbackQuery, action: str) -> bool:
    markup = callback.message.reply_markup if callback.message else None
    if markup is None:
        return False
    return any(
        (button.callback_data or "").startswith(f"{ACTION_PREFIX}:{action}:")
        for row in markup.inline_keyboard
        for button in row
    )


@router.callback_query(F.data.startswith(f"{ACTION_PREFIX}:"))
async def on_action(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
    request_id: str,
) -> None:
    parsed = parse_action(callback.data or "")
    if parsed is None:
        await callback.answer()
        return
    action, dialog_id = parsed

    # Кнопка исчезает после нажатия, но два быстрых тапа успевают проскочить
    # оба: Telegram шлёт апдейты параллельно, и второй придёт с ещё живой
    # клавиатурой. Замок в Redis — единственное, что делает «Помощь» реально
    # однократной, иначе юзер получит два сообщения и два платных вызова.
    # Префикс обязателен: redis общий с чужими проектами.
    key = get_settings().redis_key("lock", action, str(dialog_id))
    if not await queue.set(key, "1", nx=True, ex=LOCK_TTL_SEC):
        await callback.answer(ru.ACTION_ALREADY)
        log.info("повторное нажатие отбито замком", user_id=str(user.id), кнопка=action)
        return

    try:
        if action == ACTION_TEXT:
            await _show_text(callback, session, user, dialog_id)
        elif action == ACTION_HELP:
            await _show_help(callback, session, user, dialog_id)
        else:
            await callback.answer()
    except ReplyNotFound:
        log.warning("реплика для кнопки не найдена", user_id=str(user.id), реплика=dialog_id)
        await callback.answer(ru.REPLY_GONE, show_alert=True)
        await _drop_button(callback, dialog_id, action)
    except Exception:
        # Замок снимаем только при аварии: иначе юзер до истечения TTL не смог бы
        # повторить то, что не сработало. При удачном нажатии кнопка исчезает
        # сама, и замок дожидается конца TTL без вреда.
        await queue.delete(key)
        raise


async def _show_text(
    callback: CallbackQuery, session: AsyncSession, user: User, dialog_id: int
) -> None:
    breakdown = await get_text_breakdown(session, user, dialog_id)
    caption = ru.BREAKDOWN.format(
        text_zh=esc(breakdown.text_zh),
        pinyin=esc(breakdown.pinyin),
        translation=esc(breakdown.translation) or ru.NO_TRANSLATION,
    )
    markup = answer_keyboard(dialog_id, with_text=False, with_help=_has(callback, ACTION_HELP))
    try:
        # Разбор дописывается в подпись самого голосового: он не может
        # разъехаться с ответом, даже если сверху уже прилетели новые реплики.
        await callback.message.edit_caption(caption=caption[:1024], reply_markup=markup)
    except TelegramBadRequest as exc:
        log.warning("не удалось дописать разбор в подпись", причина=str(exc))
        await callback.message.answer(caption)
        await _drop_button(callback, dialog_id, ACTION_TEXT)
    await callback.answer()
    log.info("текст показан", user_id=str(user.id), реплика=dialog_id)


async def _show_help(
    callback: CallbackQuery, session: AsyncSession, user: User, dialog_id: int
) -> None:
    await callback.answer()
    # Подбор вариантов — платный вызов на несколько секунд, юзеру нужен признак
    # жизни: без него кнопка выглядит нажатой впустую.
    await callback.message.bot.send_chat_action(callback.message.chat.id, "typing")
    items, from_cache = await get_suggestions(session, user, dialog_id)
    # Подсказка длинная и в подпись голосового не влезет — отдельным сообщением,
    # ответом на то самое голосовое, чтобы связь была видна.
    await callback.message.reply(_render_help(items))
    await _drop_button(callback, dialog_id, ACTION_HELP)
    log.info(
        "подсказка показана",
        user_id=str(user.id),
        реплика=dialog_id,
        вариантов=len(items),
        из_кэша=from_cache,
    )
