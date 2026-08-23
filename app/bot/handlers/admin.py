"""Админка: `/admin` для владельца, всем остальным её не существует.

Хендлер только разбирает нажатия и рисует экраны. Числа считает
`app/core/services/admin.py`, статистику — `stats.py`, рассылку выполняет
воркер: тысяча сообщений в обработчике апдейта заблокировала бы бота целиком.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin import (
    SECTION_ADMINS,
    SECTION_BROADCAST,
    SECTION_CANCEL,
    SECTION_KNOB,
    SECTION_LIMITS,
    SECTION_NOOP,
    SECTION_PLAN,
    SECTION_PRICE,
    SECTION_SEND,
    SECTION_SPEND,
    SECTION_STATS,
    admin_menu,
    admins_keyboard,
    confirm_keyboard,
    knob_keyboard,
    limits_keyboard,
    parse_admin_action,
    plan_keyboard,
    price_keyboard,
    segments_keyboard,
    spend_keyboard,
)
from app.bot.render import plural, render_limits, render_price, render_spending, render_stats
from app.bot.texts import ru
from app.core.events import track
from app.core.services.admin import (
    KNOBS_BY_KEY,
    SEGMENT_TITLES,
    add_admin,
    admin_ids,
    admin_names,
    audience_size,
    change_limit,
    change_price,
    config_admins,
    current_value,
    editable_plans,
    is_admin,
    remove_admin,
    resolve_telegram_id,
)
from app.core.services.limits import get_limits
from app.core.services.stats import load_spending, load_summary
from app.db.models import User
from app.logging import get_logger

router = Router(name="admin")
log = get_logger("bot")

# Потолок Telegram на одно сообщение. Проверяем до постановки в очередь: узнать
# о том, что текст не влез, из отчёта о неудавшейся рассылке — поздно.
MAX_BROADCAST_LEN = 4096


def _people(count: int) -> str:
    return plural(count, "человеку", "людям", "людям")


class Broadcast(StatesGroup):
    """Ожидание текста рассылки. Состояние только у админа: попасть в него
    больше неоткуда."""

    text = State()


class AdminFilter(Filter):
    """Пропускает только админов.

    Сессию фильтр получает из данных апдейта — её кладёт та же middleware, что
    и пользователя. Без базы обойтись нельзя: часть админов выдана командой, а
    не прописана в конфиге.
    """

    async def __call__(self, event: TelegramObject, session: AsyncSession) -> bool:
        who = getattr(event, "from_user", None)
        return await is_admin(session, who.id if who else None)


# Кнопки админки чужому не отвечают вовсе: callback_data подобрать несложно.
router.callback_query.filter(AdminFilter())


async def _redraw(callback: CallbackQuery, text: str, markup) -> None:
    """Перерисовать экран на месте: админка живёт одним сообщением."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        log.debug("экран админки не изменился", причина=str(exc))


async def _pulse(session: AsyncSession) -> str:
    summary = await load_summary(session)
    return "\n\n".join(
        [
            ru.ADMIN_HEADER,
            ru.ADMIN_PULSE.format(
                users=summary.users_total,
                paying=summary.paying,
                active=summary.active_today,
            ),
        ]
    )


@router.message(Command("admin"), AdminFilter())
async def cmd_admin(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(await _pulse(session), reply_markup=admin_menu())
    await track(session, "admin_opened", user_id=user.id)
    log.info("открыта админка", user_id=str(user.id), telegram_id=message.from_user.id)


@router.message(Command("admin"))
async def cmd_admin_denied(message: Message, session: AsyncSession, user: User) -> None:
    """Чужому `/admin` бот не отвечает ничего.

    Молчание намеренное: любой ответ, даже «нет доступа», подтверждает, что
    админка существует. Ловим команду здесь, а не отдаём разговорному
    роутеру, — иначе тот вежливо сообщит, что не понял формат.
    """
    await track(session, "admin_denied", user_id=user.id, telegram_id=message.from_user.id)
    log.warning(
        "попытка войти в админку",
        user_id=str(user.id),
        telegram_id=message.from_user.id,
        username=f"@{message.from_user.username or '—'}",
    )


async def _show_admins(callback: CallbackQuery, session: AsyncSession) -> None:
    await _redraw(callback, await _admins_text(session), admins_keyboard())


async def _admins_text(session: AsyncSession) -> str:
    ids = await admin_ids(session)
    имена = await admin_names(session, ids)
    из_конфига = config_admins()
    строки = [
        ru.ADMIN_ADMINS_ROW.format(
            who=f"{имена[i]} ({i})" if i in имена else str(i),
            fixed=ru.ADMIN_ADMINS_FIXED if i in из_конфига else "",
        )
        for i in sorted(ids)
    ]
    return ru.ADMIN_ADMINS.format(list="\n".join(строки))


def _who(telegram_id: int, имена: dict[int, str]) -> str:
    return f"{имена[telegram_id]} ({telegram_id})" if telegram_id in имена else str(telegram_id)


@router.message(Command("admin_add"), AdminFilter())
async def cmd_admin_add(message: Message, session: AsyncSession, user: User) -> None:
    """Выдать доступ. Самое опасное действие в боте, поэтому в лог — warning."""
    аргумент = (message.text or "").partition(" ")[2].strip()
    if not аргумент:
        await message.answer(ru.ADMIN_ADD_USAGE)
        return

    telegram_id = await resolve_telegram_id(session, аргумент)
    if telegram_id is None:
        await message.answer(ru.ADMIN_ADD_UNKNOWN)
        log.info("кандидат в админы не найден", запрос=аргумент)
        return

    имена = await admin_names(session, {telegram_id})
    добавлен = await add_admin(session, telegram_id, by=message.from_user.id)
    шаблон = ru.ADMIN_ADD_DONE if добавлен else ru.ADMIN_ADD_ALREADY
    await message.answer(шаблон.format(who=_who(telegram_id, имена)))
    if добавлен:
        log.warning(
            "новый админ добавлен из бота",
            user_id=str(user.id),
            кому=telegram_id,
            кем=message.from_user.id,
        )


@router.message(Command("admin_del"), AdminFilter())
async def cmd_admin_del(message: Message, session: AsyncSession, user: User) -> None:
    аргумент = (message.text or "").partition(" ")[2].strip()
    if not аргумент:
        await message.answer(ru.ADMIN_DEL_USAGE)
        return

    telegram_id = await resolve_telegram_id(session, аргумент)
    if telegram_id is None:
        await message.answer(ru.ADMIN_ADD_UNKNOWN)
        return

    имена = await admin_names(session, {telegram_id})
    убран = await remove_admin(session, telegram_id, by=message.from_user.id)
    шаблон = ru.ADMIN_DEL_DONE if убран else ru.ADMIN_DEL_FIXED
    await message.answer(шаблон.format(who=_who(telegram_id, имена)))
    log.warning(
        "правка списка админов",
        user_id=str(user.id),
        кому=telegram_id,
        убран=убран,
        кем=message.from_user.id,
    )


@router.message(Command("admin_add", "admin_del"))
async def cmd_admin_grant_denied(message: Message, session: AsyncSession, user: User) -> None:
    """Чужому — то же молчание, что и на `/admin`."""
    await track(session, "admin_denied", user_id=user.id, telegram_id=message.from_user.id)
    log.warning(
        "попытка раздать права админа",
        user_id=str(user.id),
        telegram_id=message.from_user.id,
        команда=message.text,
    )


# --- разделы -------------------------------------------------------------------


async def _show_limits(callback: CallbackQuery, session: AsyncSession) -> None:
    limits = await get_limits(session)
    await _redraw(callback, render_limits(limits), limits_keyboard(limits))


async def _show_knob(callback: CallbackQuery, session: AsyncSession, key: str) -> None:
    knob = KNOBS_BY_KEY.get(key)
    if knob is None:
        await _show_limits(callback, session)
        return
    limits = await get_limits(session)
    await _redraw(
        callback,
        f"🎚 <b>{knob.title}</b>\n\n{ru.ADMIN_LIMITS}",
        knob_keyboard(knob, current_value(limits, key)),
    )


async def _show_price(callback: CallbackQuery, session: AsyncSession) -> None:
    plans = await editable_plans(session)
    await _redraw(callback, render_price(plans), price_keyboard(plans))


async def _show_spend(callback: CallbackQuery, session: AsyncSession, days: int) -> None:
    spending = await load_spending(session, days)
    await _redraw(callback, render_spending(spending, days), spend_keyboard(days))


async def _show_segments(callback: CallbackQuery, session: AsyncSession) -> None:
    sizes = {code: await audience_size(session, code) for code in SEGMENT_TITLES}
    await _redraw(callback, ru.ADMIN_BROADCAST, segments_keyboard(sizes))


@router.callback_query(F.data.startswith("adm:"))
async def on_admin_action(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
    state: FSMContext,
    request_id: str,
) -> None:
    action = parse_admin_action(callback.data or "")
    if action is None:
        await callback.answer()
        return

    # Часики гасим сразу: Telegram даёт на ответ считаные секунды, а впереди
    # запросы к базе.
    await callback.answer()

    if action.section == SECTION_NOOP:
        return

    if action.section == SECTION_STATS:
        summary = await load_summary(session)
        await _redraw(callback, render_stats(summary), admin_menu())
        return

    if action.section == SECTION_SPEND:
        await _show_spend(callback, session, int(action.value or 7))
        return

    if action.section == SECTION_LIMITS:
        await _show_limits(callback, session)
        return

    if action.section == SECTION_KNOB:
        if action.delta:
            await change_limit(session, action.value, action.delta)
        await _show_knob(callback, session, action.value)
        return

    if action.section == SECTION_PRICE:
        await _show_price(callback, session)
        return

    if action.section == SECTION_PLAN:
        if action.delta:
            await change_price(session, action.value, action.delta)
        plan = next((p for p in await editable_plans(session) if p.code == action.value), None)
        if plan is None:
            await _show_price(callback, session)
            return
        await _redraw(callback, render_price([plan]), plan_keyboard(plan))
        return

    if action.section == SECTION_ADMINS:
        await _show_admins(callback, session)
        return

    if action.section == SECTION_BROADCAST:
        if not action.value:
            await _show_segments(callback, session)
            return
        await _ask_text(callback, session, state, action.value)
        return

    if action.section == SECTION_SEND:
        await _start_broadcast(callback, session, user, queue, state, action.value, request_id)
        return

    if action.section == SECTION_CANCEL:
        await state.clear()
        await _redraw(callback, ru.ADMIN_BROADCAST_CANCELLED, admin_menu())
        log.info("рассылка отменена", user_id=str(user.id))
        return

    await _redraw(callback, await _pulse(session), admin_menu())


# --- рассылка ------------------------------------------------------------------


async def _ask_text(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, segment: str
) -> None:
    count = await audience_size(session, segment)
    title = SEGMENT_TITLES.get(segment, segment)
    if not count:
        # Сегмент опустел, пока админ выбирал: перерисовываем его же экран с
        # честными числами, а не пустой клавиатурой из нулей.
        sizes = {code: await audience_size(session, code) for code in SEGMENT_TITLES}
        await _redraw(callback, ru.ADMIN_BROADCAST_EMPTY, segments_keyboard(sizes))
        return
    await state.set_state(Broadcast.text)
    await state.update_data(segment=segment)
    await _redraw(
        callback,
        ru.ADMIN_BROADCAST_ASK.format(segment=title, count=_people(count)),
        None,
    )


@router.message(Broadcast.text, F.text.startswith("/"), AdminFilter())
async def cancel_broadcast(message: Message, state: FSMContext) -> None:
    """Любая команда отменяет набор текста.

    Иначе `/start`, набранный по привычке, уехал бы всем адресатам как текст
    рассылки — а отменить отправленное нечем.
    """
    await state.clear()
    await message.answer(ru.ADMIN_BROADCAST_CANCELLED)
    log.info("набор текста рассылки прерван командой", команда=message.text)


@router.message(Broadcast.text, F.text, AdminFilter())
async def got_broadcast_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """Текст рассылки. Показываем ровно то, что уйдёт людям.

    Берём `html_text`, а не голый текст: жирный шрифт и ссылки, набранные
    админом, обязаны доехать до адресатов, а разметка у бота HTML.
    """
    body = message.html_text
    if len(body) > MAX_BROADCAST_LEN:
        await message.answer(ru.ADMIN_BROADCAST_TOO_LONG)
        return

    data = await state.get_data()
    segment = data.get("segment", "")
    count = await audience_size(session, segment)
    await state.update_data(text=body)

    await message.answer(ru.ADMIN_BROADCAST_PREVIEW.format(text=body))
    await message.answer(
        ru.ADMIN_BROADCAST_CONFIRM.format(
            count=_people(count), segment=SEGMENT_TITLES.get(segment, segment)
        ),
        reply_markup=confirm_keyboard(segment),
    )
    log.info("текст рассылки принят", сегмент=segment, знаков=len(body), адресатов=count)


async def _start_broadcast(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
    state: FSMContext,
    segment: str,
    request_id: str,
) -> None:
    data = await state.get_data()
    body = data.get("text")
    if not body:
        await _redraw(callback, ru.ADMIN_BROADCAST_CANCELLED, admin_menu())
        await state.clear()
        return

    count = await audience_size(session, segment)
    await state.clear()
    await queue.enqueue_job(
        "run_broadcast",
        segment=segment,
        text=body,
        admin_chat_id=callback.message.chat.id,
        request_id=request_id,
    )
    await _redraw(callback, ru.ADMIN_BROADCAST_STARTED.format(count=_people(count)), None)
    await track(session, "broadcast_started", user_id=user.id, сегмент=segment, адресатов=count)
    log.info(
        "рассылка поставлена в очередь",
        user_id=str(user.id),
        сегмент=segment,
        адресатов=count,
        знаков=len(body),
    )
