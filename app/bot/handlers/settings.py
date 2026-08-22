"""Раздел «Настройки» и кнопки нижнего меню.

Хендлер разбирает нажатие и рисует экран, всё остальное делает
`app/core/services/settings.py`: тот же набор настроек потом откроет вебапп.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.subscription import show_subscription
from app.bot.keyboards.menu import main_menu
from app.bot.keyboards.settings import (
    ACTION_PLAY,
    SECTION_LEVEL,
    SECTION_SPEED,
    SECTION_TOPIC,
    SECTION_VOICE,
    SET_PREFIX,
    level_keyboard,
    parse_settings_action,
    settings_menu,
    speed_keyboard,
    topic_keyboard,
    voice_keyboard,
)
from app.bot.texts import ru
from app.config import get_settings
from app.core.events import track
from app.core.services.dialog import PROMPT_TOPIC_START, set_hsk_level
from app.core.services.settings import (
    TOPIC_FREE_CODE,
    current_level,
    current_speed,
    current_topic,
    current_voice,
    set_speed,
    set_topic,
    set_voice,
    voices,
)
from app.db.models import User
from app.logging import get_logger

router = Router(name="settings")
log = get_logger("bot")

# Образец голоса — платный вызов озвучки. Замок нужен ровно за этим: без него
# десять быстрых нажатий превращаются в десять оплаченных синтезов.
PREVIEW_LOCK_TTL_SEC = 15


def screen_text(user: User) -> str:
    voice = current_voice(user)
    return ru.SETTINGS_HEADER.format(
        level=ru.LEVEL_TITLES[current_level(user)],
        voice=voice.title if voice else ru.SETTINGS_VOICE_DEFAULT,
        speed=current_speed(user).title,
        topic=current_topic(user).title,
    )


async def show_settings(message: Message, user: User) -> None:
    await message.answer(screen_text(user), reply_markup=settings_menu())


async def _redraw(callback: CallbackQuery, text: str, markup) -> None:
    """Перерисовать экран на месте.

    Раздел живёт одним сообщением: иначе после пяти нажатий переписка
    превращается в столбик одинаковых экранов.
    """
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # Тот же текст и та же клавиатура — значит нажали то, что уже выбрано.
        log.debug("экран настроек не изменился", причина=str(exc))


@router.message(Command("settings"))
@router.message(F.text == ru.MENU_PROFILE)
async def cmd_settings(message: Message, session: AsyncSession, user: User) -> None:
    await show_settings(message, user)
    await track(session, "settings_opened", user_id=user.id)
    log.info("открыт раздел настроек", user_id=str(user.id), уровень=current_level(user))


@router.message(F.text == ru.MENU_TALK)
async def menu_talk(message: Message, user: User) -> None:
    """«Общение» — состояние по умолчанию, отдельного экрана у него нет."""
    await message.answer(ru.TALK_HINT, reply_markup=main_menu())


@router.message(F.text == ru.MENU_SUBSCRIPTION)
async def menu_subscription(message: Message, session: AsyncSession, user: User) -> None:
    await show_subscription(message, session, user)
    await track(session, "subscription_opened", user_id=user.id, источник="нижнее меню")


async def _open_section(callback: CallbackQuery, user: User, section: str) -> None:
    if section == SECTION_LEVEL:
        await _redraw(callback, ru.SETTINGS_LEVEL, level_keyboard(current_level(user)))
    elif section == SECTION_VOICE:
        catalogue = voices()
        if not catalogue:
            await _redraw(callback, ru.SETTINGS_VOICE_EMPTY, settings_menu())
            return
        voice = current_voice(user)
        await _redraw(
            callback, ru.SETTINGS_VOICE, voice_keyboard(catalogue, voice.id if voice else None)
        )
    elif section == SECTION_SPEED:
        await _redraw(callback, ru.SETTINGS_SPEED, speed_keyboard(current_speed(user).code))
    elif section == SECTION_TOPIC:
        await _redraw(callback, ru.SETTINGS_TOPIC, topic_keyboard(current_topic(user).code))
    else:
        await _redraw(callback, screen_text(user), settings_menu())


async def _take_lock(queue, user: User, name: str) -> bool:
    """Замок на платное нажатие. False — недавно уже жали.

    Прослушивание образца и первая фраза по новой теме — оба вызывают озвучку,
    а она стоит денег. Без замка десять быстрых тапов дают десять оплаченных
    синтезов и десять голосовых подряд.
    """
    key = get_settings().redis_key("lock", name, str(user.id))
    return bool(await queue.set(key, "1", nx=True, ex=PREVIEW_LOCK_TTL_SEC))


@router.callback_query(F.data.startswith(f"{SET_PREFIX}:"))
async def on_settings_action(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
    request_id: str,
) -> None:
    action = parse_settings_action(callback.data or "")
    if action is None:
        await callback.answer()
        return

    # Прослушивание — платный синтез, поэтому уходит в очередь и под замок.
    if action.section == SECTION_VOICE and action.action == ACTION_PLAY:
        if not await _take_lock(queue, user, "voice_preview"):
            await callback.answer(ru.ACTION_ALREADY)
            log.info("повторное нажатие образца отбито", user_id=str(user.id))
            return
        await callback.answer()
        await queue.enqueue_job(
            "preview_voice",
            user_id=str(user.id),
            chat_id=callback.message.chat.id,
            voice_id=action.value,
            request_id=request_id,
        )
        await track(session, "voice_sample", user_id=user.id, голос=action.value)
        log.info("образец голоса поставлен в очередь", user_id=str(user.id), голос=action.value)
        return

    # Часики гасим до работы: Telegram даёт на ответ считаные секунды.
    await callback.answer(ru.SETTINGS_SAVED if action.value else None)

    if not action.value:
        await _open_section(callback, user, action.section)
        return

    if action.section == SECTION_LEVEL:
        await set_hsk_level(session, user, action.value)
        await _redraw(callback, ru.SETTINGS_LEVEL, level_keyboard(current_level(user)))
        return

    if action.section == SECTION_VOICE:
        await set_voice(session, user, action.value)
        voice = current_voice(user)
        await _redraw(
            callback, ru.SETTINGS_VOICE, voice_keyboard(voices(), voice.id if voice else None)
        )
        return

    if action.section == SECTION_SPEED:
        chosen = await set_speed(session, user, action.value)
        await _redraw(callback, ru.SETTINGS_SPEED, speed_keyboard(chosen.code))
        return

    if action.section == SECTION_TOPIC:
        chosen = await set_topic(session, user, action.value)
        await _redraw(callback, ru.SETTINGS_TOPIC, topic_keyboard(chosen.code))
        if chosen.code == TOPIC_FREE_CODE:
            await callback.message.answer(ru.TOPIC_SET_FREE)
            return
        # Тема — не просто пометка в базе: бот сам начинает разговор по ней,
        # иначе смена темы ничем не отличается от ничего. Первая фраза платная,
        # поэтому под тем же замком, что и образец голоса.
        if not await _take_lock(queue, user, "topic_greet"):
            await callback.message.answer(ru.TOPIC_SET_QUIET.format(title=chosen.title))
            return
        await callback.message.answer(ru.TOPIC_SET.format(title=chosen.title))
        await queue.enqueue_job(
            "greet_user",
            user_id=str(user.id),
            chat_id=callback.message.chat.id,
            request_id=request_id,
            prompt_code=PROMPT_TOPIC_START,
        )
        return

    await _redraw(callback, screen_text(user), settings_menu())
