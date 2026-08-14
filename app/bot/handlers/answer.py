"""Кнопки под голосовым ответом бота: «Текст», «Помощь», «Оценка»."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.answer import (
    ACTION_AGAIN,
    ACTION_CANCEL,
    ACTION_HELP,
    ACTION_LISTEN,
    ACTION_PREFIX,
    ACTION_PRON,
    ACTION_TEXT,
    answer_keyboard,
    parse_action,
    practice_keyboard,
)
from app.bot.render import esc, render_practice
from app.bot.texts import ru
from app.config import get_settings
from app.core.events import track
from app.core.services.breakdown import (
    ReplyNotFound,
    Suggestion,
    get_suggestions,
    get_text_breakdown,
)
from app.core.services.pronunciation import (
    PracticeTarget,
    choose_target,
    load_practice,
    remember_reference_audio,
    start_practice,
    stop_practice,
)
from app.db.models import User
from app.logging import get_logger

router = Router(name="answer")
log = get_logger("bot")

# Замок живёт минуту там, где нажатие стоит денег: этого хватает на самый
# медленный вызов и не мешает осмысленному повтору. Кнопки, которые ничего не
# считают и живут под сообщением постоянно («Послушать», «Ещё раз»), держатся
# секунды — иначе юзер, послушавший эталон, не смог бы послушать его снова.
LOCK_TTL_SEC = 60
QUICK_LOCK_TTL_SEC = 5
QUICK_ACTIONS = {ACTION_LISTEN, ACTION_AGAIN, ACTION_CANCEL}

# Кнопки, которые после нажатия исчезают: их результат остаётся на экране, и
# нажимать второй раз нечего.
ONE_SHOT_ACTIONS = {ACTION_TEXT, ACTION_HELP, ACTION_PRON}


def _render_help(items: list[Suggestion]) -> str:
    if not items:
        return ru.HELP_EMPTY
    blocks = [
        ru.HELP_ITEM.format(n=i, zh=esc(s.zh), pinyin=esc(s.pinyin), ru=esc(s.ru) or "—")
        for i, s in enumerate(items, 1)
    ]
    return "\n\n".join([ru.HELP_HEADER, *blocks, ru.HELP_FOOTER])


async def _drop_button(callback: CallbackQuery, dialog_id: int, action: str) -> None:
    """Убрать нажатую кнопку, оставив остальные."""
    markup = answer_keyboard(
        dialog_id,
        with_text=action != ACTION_TEXT and _has(callback, ACTION_TEXT),
        with_help=action != ACTION_HELP and _has(callback, ACTION_HELP),
        with_pron=action != ACTION_PRON and _has(callback, ACTION_PRON),
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
    # клавиатурой. Замок в Redis — единственное, что делает платное нажатие
    # реально однократным, иначе юзер получит два сообщения и два вызова.
    # Префикс обязателен: redis общий с чужими проектами.
    key = get_settings().redis_key("lock", action, str(dialog_id))
    ttl = QUICK_LOCK_TTL_SEC if action in QUICK_ACTIONS else LOCK_TTL_SEC
    if not await queue.set(key, "1", nx=True, ex=ttl):
        await callback.answer(ru.ACTION_ALREADY)
        log.info("повторное нажатие отбито замком", user_id=str(user.id), кнопка=action)
        return

    # Часики гасим ДО работы и ровно один раз. Telegram даёт на ответ секунды:
    # на живой проверке правка подписи по холодному соединению заняла 37 секунд,
    # запоздалый ответ отлетел с «query is too old», юзер всё это время смотрел
    # на крутилку, а хендлер падал с ошибкой.
    await callback.answer()

    try:
        if action == ACTION_TEXT:
            await _show_text(callback, session, user, dialog_id)
        elif action == ACTION_HELP:
            await _show_help(callback, session, user, dialog_id)
        elif action == ACTION_PRON:
            await _start_practice(callback, session, user, queue, dialog_id)
        elif action == ACTION_LISTEN:
            await _listen_again(callback, session, user, queue, dialog_id)
        elif action == ACTION_AGAIN:
            await _try_again(callback, session, user, queue, dialog_id)
        elif action == ACTION_CANCEL:
            await _cancel_practice(callback, session, user, queue, dialog_id)
    except ReplyNotFound:
        log.warning("реплика для кнопки не найдена", user_id=str(user.id), реплика=dialog_id)
        # Всплывашкой уже не ответить — запрос закрыт выше, поэтому обычным
        # сообщением.
        практика = action == ACTION_PRON or action in QUICK_ACTIONS
        await callback.message.answer(ru.PRACTICE_GONE if практика else ru.REPLY_GONE)
        if action in ONE_SHOT_ACTIONS:
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
    markup = answer_keyboard(
        dialog_id,
        with_text=False,
        with_help=_has(callback, ACTION_HELP),
        with_pron=_has(callback, ACTION_PRON),
    )
    try:
        # Разбор дописывается в подпись самого голосового: он не может
        # разъехаться с ответом, даже если сверху уже прилетели новые реплики.
        await callback.message.edit_caption(caption=caption[:1024], reply_markup=markup)
    except TelegramBadRequest as exc:
        log.warning("не удалось дописать разбор в подпись", причина=str(exc))
        await callback.message.answer(caption)
        await _drop_button(callback, dialog_id, ACTION_TEXT)
    log.info("текст показан", user_id=str(user.id), реплика=dialog_id)


async def _show_help(
    callback: CallbackQuery, session: AsyncSession, user: User, dialog_id: int
) -> None:
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


async def _send_reference(
    callback: CallbackQuery, user: User, queue, target: PracticeTarget
) -> None:
    """Отправить эталон голосом. Синтезируем только когда переслать нечего.

    У реплики бота голос уже лежит в Telegram — пересылка по file_id мгновенна
    и бесплатна. Синтез нужен исправленной фразе: её вслух ещё никто не говорил.
    """
    bot = callback.message.bot
    chat_id = callback.message.chat.id
    caption = render_practice(target)[:1024]
    markup = practice_keyboard(target.dialog_id)

    if target.audio_file_id:
        await bot.send_voice(chat_id, target.audio_file_id, caption=caption, reply_markup=markup)
        return

    from app.core.services.dialog import synthesize_voice

    await bot.send_chat_action(chat_id, "record_voice")
    audio = await synthesize_voice(target.ref_text, user, get_settings())
    message = await bot.send_voice(
        chat_id,
        BufferedInputFile(audio, filename="reference.ogg"),
        caption=caption,
        reply_markup=markup,
    )
    if message.voice:
        # Второй раз ту же фразу синтезировать незачем: «Послушать эталон»
        # заберёт готовый file_id из состояния тренировки.
        await remember_reference_audio(queue, user, message.voice.file_id)


async def _start_practice(
    callback: CallbackQuery, session: AsyncSession, user: User, queue, dialog_id: int
) -> None:
    target = await choose_target(session, user, dialog_id)
    await start_practice(queue, user, target)
    await _send_reference(callback, user, queue, target)
    await _drop_button(callback, dialog_id, ACTION_PRON)
    await track(
        session,
        "practice_started",
        user_id=user.id,
        реплика=dialog_id,
        из_исправления=target.from_correction,
    )
    log.info(
        "эталон отправлен",
        user_id=str(user.id),
        реплика=dialog_id,
        эталон=target.ref_text,
        из_исправления=target.from_correction,
    )


async def _resolve_target(
    session: AsyncSession, user: User, queue, dialog_id: int
) -> PracticeTarget:
    """Взять фразу из режима, а если он истёк — собрать её заново по реплике."""
    target = await load_practice(queue, user)
    if target is not None and target.dialog_id == dialog_id:
        return target
    return await choose_target(session, user, dialog_id)


async def _listen_again(
    callback: CallbackQuery, session: AsyncSession, user: User, queue, dialog_id: int
) -> None:
    target = await _resolve_target(session, user, queue, dialog_id)
    await _send_reference(callback, user, queue, target)
    log.info("эталон переслан", user_id=str(user.id), реплика=dialog_id)


async def _try_again(
    callback: CallbackQuery, session: AsyncSession, user: User, queue, dialog_id: int
) -> None:
    target = await _resolve_target(session, user, queue, dialog_id)
    # Режим гаснет после каждой оценки: иначе юзер, вернувшийся к разговору,
    # отправлял бы реплики на проверку произношения. «Ещё раз» включает его
    # обратно, ничего не переозвучивая.
    await start_practice(queue, user, target)
    await callback.message.answer(
        ru.PRACTICE_WAITING.format(text_zh=esc(target.ref_text)),
        reply_markup=practice_keyboard(dialog_id),
    )
    await track(session, "practice_retry", user_id=user.id, реплика=dialog_id)
    log.info("новая попытка запрошена", user_id=str(user.id), реплика=dialog_id)


async def _cancel_practice(
    callback: CallbackQuery, session: AsyncSession, user: User, queue, dialog_id: int
) -> None:
    await stop_practice(queue, user)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as exc:
        log.debug("клавиатура тренировки уже убрана", причина=str(exc))
    await callback.message.answer(ru.PRACTICE_CANCELLED)
    await track(session, "practice_cancelled", user_id=user.id, реплика=dialog_id)
